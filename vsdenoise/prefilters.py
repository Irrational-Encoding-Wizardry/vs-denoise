"""
This module implements prefilters for denoisers
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Type

import vapoursynth as vs
from vsrgtools import box_blur, gauss_blur, min_blur, replace_low_frequencies
from vsrgtools.util import PlanesT, norm_expr_planes, normalise_planes, wmean_matrix
from vsutil import Dither
from vsutil import Range as CRange
from vsutil import (
    depth, disallow_variable_format, disallow_variable_resolution, get_depth, get_neutral_value, get_peak_value, get_y,
    scale_value
)

from .bm3d import BM3D as BM3DM
from .bm3d import BM3DCPU, AbstractBM3D, BM3DCuda, BM3DCudaRTC, Profile
from .knlm import knl_means_cl
from .utils import planes_to_channelmode

core = vs.core


class Prefilter(IntEnum):
    AUTO = -2
    NONE = -1
    MINBLUR1 = 0
    MINBLUR2 = 1
    MINBLUR3 = 2
    MINBLURFLUX = 3
    DFTTEST = 4
    KNLMEANSCL = 5
    BM3D = 6
    BM3D_CPU = 7
    BM3D_CUDA = 8
    BM3D_CUDA_RTC = 9
    DGDENOISE = 10
    HALFBLUR = 11
    GAUSSBLUR1 = 12
    GAUSSBLUR2 = 13


@disallow_variable_format
@disallow_variable_resolution
def prefilter_clip(clip: vs.VideoNode, pref_type: Prefilter, planes: PlanesT = None, **kwargs: Any) -> vs.VideoNode:
    pref_type = Prefilter.MINBLUR3 if pref_type == Prefilter.AUTO else pref_type

    bits = get_depth(clip)
    peak = get_peak_value(clip)
    planes = normalise_planes(clip, planes)

    if pref_type == Prefilter.NONE:
        return clip
    elif pref_type.value in {0, 1, 2}:
        return min_blur(clip, pref_type.value, planes)
    elif pref_type == Prefilter.MINBLURFLUX:
        return min_blur(clip, 2, planes).flux.SmoothST(2, 2, planes)
    elif pref_type == Prefilter.DFTTEST:
        dftt_args = dict[str, Any](
            tbsize=1, sbsize=12, sosize=6, swin=2, slocation=[
                0.0, 4.0, 0.2, 9.0, 1.0, 15.0
            ]
        ) | kwargs

        dfft = clip.dfttest.DFTTest(**dftt_args)

        i, j = (scale_value(x, 8, bits, range=CRange.FULL) for x in (16, 75))

        pref_mask = get_y(clip).std.Expr(
            f'x {i} < {peak} x {j} > 0 {peak} x {i} - {peak} {j} {i} - / * - ? ?'
        )

        return dfft.std.MaskedMerge(clip, pref_mask, planes)
    elif pref_type == Prefilter.KNLMEANSCL:
        knl = knl_means_cl(clip, 7.0, 1, 2, 2, planes_to_channelmode(planes), **kwargs)

        return replace_low_frequencies(knl, clip, 600 * (clip.width / 1920), False, planes)
    elif pref_type in {Prefilter.BM3D, Prefilter.BM3D_CPU, Prefilter.BM3D_CUDA, Prefilter.BM3D_CUDA_RTC}:
        bm3d_arch: Type[AbstractBM3D]

        if pref_type == Prefilter.BM3D:
            bm3d_arch, sigma, profile = BM3DM, 10, Profile.FAST
        elif pref_type == Prefilter.BM3D_CPU:
            bm3d_arch, sigma, profile = BM3DCPU, 10, Profile.LOW_COMPLEXITY
        elif pref_type == Prefilter.BM3D_CUDA:
            bm3d_arch, sigma, profile = BM3DCuda, 8, Profile.NORMAL
        elif pref_type == Prefilter.BM3D_CUDA_RTC:
            bm3d_arch, sigma, profile = BM3DCudaRTC, 8, Profile.NORMAL
        else:
            raise ValueError

        sigmas = [sigma if 0 in planes else 0, sigma if (1 in planes or 2 in planes) else 0]

        bm3d_args = dict[str, Any](sigma=sigmas, radius=1, profile=profile) | kwargs

        return bm3d_arch(clip, **bm3d_args).clip
    elif pref_type == Prefilter.DGDENOISE:
        # dgd = core.dgdecodenv.DGDenoise(pref, 0.10)

        # pref = replace_low_frequencies(dgd, pref, w / 2)
        return gauss_blur(clip, 1, planes=planes, **kwargs)
    elif pref_type == Prefilter.HALFBLUR:
        half_clip = clip.resize.Bilinear(clip.width // 2, clip.height // 2)

        boxblur = box_blur(half_clip, wmean_matrix, planes, **kwargs)

        return boxblur.resize.Bilinear(clip.width, clip.height)
    elif pref_type in {Prefilter.GAUSSBLUR1, Prefilter.GAUSSBLUR2}:
        boxblur = box_blur(clip, wmean_matrix, planes)

        gaussblur = gauss_blur(boxblur, 1.75, planes=planes, **kwargs)

        if pref_type == Prefilter.GAUSSBLUR2:
            i2, i7 = (scale_value(x, 8, bits) for x in (2, 7))

            merge_expr = f'x {i7} + y < x {i2} + x {i7} - y > x {i2} - x 51 * y 49 * + 100 / ? ?'
        else:
            merge_expr = 'x 0.9 * y 0.1 * +'

        return core.std.Expr([gaussblur, clip], norm_expr_planes(clip, merge_expr, planes))

    return clip


def prefilter_to_full_range(
    pref: vs.VideoNode, prefilter: Prefilter, range_conversion: float
) -> vs.VideoNode:
    pref = prefilter_clip(pref, prefilter)
    fmt = pref.format
    assert fmt

    # Luma expansion TV->PC (up to 16% more values for motion estimation)
    if range_conversion > 1.0:
        is_gray = fmt.color_family == vs.GRAY
        is_integer = fmt.sample_type == vs.INTEGER

        bits = get_depth(pref)
        neutral = get_neutral_value(pref)
        max_val = get_peak_value(pref)
        min_tv_val = scale_value(16, 8, bits)
        max_tv_val = scale_value(219, 8, bits)

        c = 0.0625

        k = (range_conversion - 1) * c
        t = f'x {min_tv_val} - {max_tv_val} / 0 max 1 min' if is_integer else 'x 0 max 1 min'

        pref = pref.std.Expr([
            f"{k} {1 + c} {(1 + c) * c} {t} {c} + / - * {t} 1 {k} - * + {f'{max_val} *' if is_integer else ''}",
            f'x {neutral} - 128 * 112 / {neutral} +'
        ][:1 + (not is_gray and is_integer)])
    elif range_conversion > 0.0:
        pref = pref.retinex.MSRCP(None, range_conversion, None, False, True)
    else:
        pref = depth(
            pref, fmt.bits_per_sample,
            range=CRange.FULL, range_in=CRange.LIMITED,
            dither_type=Dither.NONE
        )

    return pref
