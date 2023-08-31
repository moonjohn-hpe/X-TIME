"""Python wrapper around CAM kernels."""

from math import ceil, prod
from typing import Optional, Tuple

import cupy as cp
from numpy.typing import DTypeLike, NDArray

from .types import CamOp, CamVariant, Kernel


def run_kernel(
    kernel: Kernel,
    variant: CamVariant,
    op: CamOp,
    inputs: NDArray,
    cam: NDArray,
    result_dtype: DTypeLike,
    reduction_values: Optional[NDArray] = None,
) -> NDArray:
    """
    This is the heart of this library's shape magic. This function does the following:

    1. Prepare the parameters needed to call a kernel generated by `generate_kernel`.
       This includes both the launch configuration and the direct kernel function
       parameters.

    2. Call the kernel

    3. Ensure that the results are returned in the correct shape.

    This function does so many things at once because they all depend on the same
    "shape logic", which is harder to get right when split up all over the place.

    The reason behind this is that the CUDA kernels themselves only support inputs with
    equal dimensions for simplicity. This function makes the necessary calculations such
    that asymmetrical dimensions are flattened down to symmetrical ones before the
    kernel call, and restored later before returning the results.
    """

    input_rows, cam_rows = inputs.shape[-2], cam.shape[-2]
    columns = cam.shape[-1] // variant.cell_encoding_width

    # while the two innermost shapes of `inputs` and `cam` are `input_rows x columns`
    # and `cam_rows x columns` respectively, the remaining outer shapes hold all the
    # information on how the inputs are stacked and to be broadcasted.
    inputs_outer_shape, cam_outer_shape = inputs.shape[:-2], cam.shape[:-2]

    # sort the outer shapes by length. we expect the smaller shape (`sub_shape`) to be
    # a subset of the larger shape (`super_shape`) as the name suggests.
    # Specifically, the only way that `super_shape` can differ from `sub_shape` is by
    # extending it to the left, i.e. adding even higher dimensions. This behavior is
    # enforced within `check_cam_params`.
    #
    # For example:
    #
    # inputs_outer_shape = (4, 3), cam_outer_shape = (5, 3)
    # --> sub_shape = (), super_shape = ()
    #
    # inputs_outer_shape = (2, 4, 3), cam_outer_shape = (2, 4, 3)
    # --> sub_shape = (2), super_shape = (2)
    #
    # inputs_outer_shape = (5, 2, 4, 3), cam_outer_shape = (2, 4, 3)
    # --> sub_shape = (2), super_shape = (5, 2)
    sub_shape, super_shape = sorted([inputs_outer_shape, cam_outer_shape], key=len)

    # the "overhang" is the difference between `sub_shape` and a super shape, i.e.
    # the dimensions that remain to the left. If both outer shapes are equal and we have
    # symmetrical dimensions, the overhang will always be empty.
    #
    # Following the previous example:
    # inputs_outer_shape = (5, 2, 4, 3), cam_outer_shape = (2, 4, 3)
    # --> sub_shape = (2), super_shape = (5, 2)
    # --> overhang(inputs_outer_shape) = (5)
    # --> overhang(cam_outer_shape) = ()
    def overhang(shape: Tuple[int, ...]) -> Tuple[int, ...]:
        return shape[: -len(sub_shape)] if sub_shape else shape

    # these values tell us by which factor to extend `input_rows` or `cam_rows`
    # respectively when calling the kernel by reducing the overhanging dimensions
    # via multiplication.
    # at least one of these will always be equal to one because the outer shape is
    # equal to `sub_shape` and thereby the overhang is empty.
    inputs_overhang = prod(overhang(inputs_outer_shape))
    cam_overhang = prod(overhang(cam_outer_shape))

    is_reduction = op.is_reduction

    results_shape = super_shape + (
        (input_rows,) if is_reduction else (input_rows, cam_rows)
    )

    # allocate the required space for the results in the shape that they will
    # finally be returned as. Even though the kernel will always interpret `results`
    # in the context of an even stack, the buffer is the same size and the shape is
    # not relevant to the CUDA code.
    results = cp.zeros(
        results_shape,
        dtype=result_dtype,  # type: ignore (https://github.com/cupy/cupy/pull/7702)
    )

    # CuPy kindly provides this information such that we don't need to hardcode / guess
    # the value.
    # Depending on the GPU, this is typically 512 or 1024.
    max_threads_per_block = kernel.attributes["max_threads_per_block"]

    # a core = a single inputs/CAM pair within the operation stack
    # each core calculates a single point in a `input_rows x cam_rows` match matrix,
    # where we need to account for overhangs generates from asymmetrical stacks.
    threads_per_core = input_rows * inputs_overhang * cam_rows * cam_overhang

    # this is the edge case where we have less threads per core than what would fill
    # a single block.
    threads_per_block = min(threads_per_core, max_threads_per_block)

    # the `x` dimensions of `dim_block` and `dim_grid` generate `threads_per_core`
    # threads.
    # the `y` dimension of `dim_grid` accounts for the amount of cores, which given
    # an even stack is equal to `prod(sub_shape)`.
    dim_block = (threads_per_block, 1, 1)
    dim_grid = (ceil(threads_per_core / threads_per_block), prod(sub_shape), 1)

    kernel_args = (
        # make sure to move inputs and CAM to the GPU, this no-ops if they
        # are already on the device
        cp.asarray(inputs.ravel()),
        cp.asarray(cam.ravel()),
        columns,
        # the CAM kernels operate on even shapes, so the overhang needs to be
        # accounted for by extending the rows.
        input_rows * inputs_overhang,
        cam_rows * cam_overhang,
        results,
    )

    if is_reduction:
        assert reduction_values is not None
        kernel_args += (cp.asarray(reduction_values.ravel()),)

    # call the CUDA kernel. this will mutate `results` in place.
    kernel(dim_grid, dim_block, kernel_args)

    # this is the final special case where the kernel has placed the correct values
    # in `results` but it needs to be rearranged such that they also appear in the
    # correct place.
    # there are three different cases for the shapes of the parameters taken by the
    # CAM functions:
    #
    # 1. The shapes of `inputs` and `cam` are equal.
    # 2. The shape of `inputs` supersets the shape of `cam`.
    # 3. The shape of `cam` supersets the shape of `inputs`.
    #
    # case 1 works fine because the kernels themselves are built to directly handle it.
    # for case 2 and 3 each, `results` needs to be reshaped and transposed such that
    # the results correctly propagate across the overhanging dimensions.
    #
    # the algorithm here is as follows:
    #
    # 1. reshape `results` such that the overhanging dimensions are moved in front of
    #    the dimension that was initially modified during the kernel call because of
    #    the overhang. For case 2, this is in front of `input_rows` and for case 3,
    #    in front of `cam_rows`.
    #
    # 2. transpose `results` such that the dimensions go back into correct order.

    # case 1, nothing needs to be done
    if len(inputs_outer_shape) == len(cam_outer_shape):
        return results

    match_dims = len(results.shape)
    sub_shape_dims = len(sub_shape)

    # case 2
    if len(inputs_outer_shape) > len(cam_outer_shape):
        # move the overhang behind the sub shape and in front of the input rows
        results = results.reshape(
            (*sub_shape, *overhang(super_shape), input_rows, cam_rows)
        )
        # transpose the dimensions back into the right order
        results = results.transpose(
            *range(sub_shape_dims, match_dims - 2),  # move overhang in front
            *range(sub_shape_dims),  # move sub shape behing overhang
            match_dims - 2,  # keep input rows in place
            match_dims - 1,  # keep cam rows in place
        )

    # case 3
    else:
        # move the overhang behind the sub shape and the input rows and in front of the
        # cam rows
        results = results.reshape(
            (*sub_shape, input_rows, *overhang(super_shape), cam_rows)
        )
        # transpose the dimensions back into the right order
        results = results.transpose(
            *range(sub_shape_dims + 1, match_dims - 1),  # move overhang in front
            *range(sub_shape_dims),  # move sub shape behind overhang
            sub_shape_dims,  # move input rows back in front of cam rows
            match_dims - 1,  # keep cam rows in place
        )

    return results
