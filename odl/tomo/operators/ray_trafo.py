# Copyright 2014-2020 The ODL contributors
#
# This file is part of ODL.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.

"""Ray transforms."""

from __future__ import absolute_import, division, print_function

import warnings
from copy import copy

import numpy as np

from odl.discr import DiscreteLp
from odl.operator import Operator
from odl.space import FunctionSpace
from odl.space.weighting import ConstWeighting
from odl.tomo.backends import (
    ASTRA_AVAILABLE, ASTRA_CUDA_AVAILABLE, ASTRA_VERSION, SKIMAGE_AVAILABLE,
    AstraCudaBackProjectorImpl, AstraCudaProjectorImpl,
    astra_cpu_back_projector, astra_cpu_forward_projector, astra_supports,
    astra_versions_supporting, skimage_radon_back_projector,
    skimage_radon_forward_projector)
from odl.tomo.geometry import (
    Geometry, Parallel2dGeometry, Parallel3dAxisGeometry)

ASTRA_CPU_AVAILABLE = ASTRA_AVAILABLE
_SUPPORTED_IMPL = ('astra_cpu', 'astra_cuda', 'skimage')
_AVAILABLE_IMPLS = []
if ASTRA_CPU_AVAILABLE:
    _AVAILABLE_IMPLS.append('astra_cpu')
if ASTRA_CUDA_AVAILABLE:
    _AVAILABLE_IMPLS.append('astra_cuda')
if SKIMAGE_AVAILABLE:
    _AVAILABLE_IMPLS.append('skimage')


__all__ = ('RayTransform', 'RayBackProjection')


class RayTransformBase(Operator):

    """Base class for ray transforms containing common attributes."""

    def __init__(self, reco_space, geometry, variant, **kwargs):
        """Initialize a new instance.

        Parameters
        ----------
        reco_space : `DiscreteLp`
            Discretized reconstruction space, the domain of the forward
            operator or the range of the adjoint (back-projection).
        geometry : `Geometry`
            Geometry of the transform that contains information about
            the data structure.
        variant : {'forward', 'backward'}
            Variant of the transform, i.e., whether the ray transform
            or its back-projection should be created.

        Other Parameters
        ----------------
        impl : {`None`, 'astra_cuda', 'astra_cpu', 'skimage'}, optional
            Implementation back-end for the transform. Supported back-ends:

            - ``'astra_cuda'``: ASTRA toolbox, using CUDA, 2D or 3D
            - ``'astra_cpu'``: ASTRA toolbox using CPU, only 2D
            - ``'skimage'``: scikit-image, only 2D parallel with square
              reconstruction space.

            For the default ``None``, the fastest available back-end is
            used.

        proj_space : `DiscreteLp`, optional
            Discretized projection (sinogram) space, the range of the forward
            operator or the domain of the adjoint (back-projection).
            Default: Inferred from parameters.
        use_cache : bool, optional
            If ``True``, data is cached. This gives a significant speed-up
            at the expense of a notable memory overhead, both on the GPU
            and on the CPU, since a full volume and a projection dataset
            are stored. That may be prohibitive in 3D.
            Default: True
        gpu_index : int, optional
            Index of GPU to use for ``impl='astra_cuda'``.
            Default: ``0``
        kwargs
            Further keyword arguments passed to the projector backend.

        Notes
        -----
        The ASTRA backend is faster if data are given with
        ``dtype='float32'`` and storage order 'C'. Otherwise copies will be
        needed.
        """
        variant, variant_in = str(variant).lower(), variant
        if variant not in ('forward', 'backward'):
            raise ValueError('`variant` {!r} not understood'
                             ''.format(variant_in))

        if variant == 'forward':
            reco_name = 'domain'
            proj_name = 'range'
        else:
            reco_name = 'range'
            proj_name = 'domain'

        if not isinstance(reco_space, DiscreteLp):
            raise TypeError('`{}` must be a `DiscreteLp` instance, got '
                            '{!r}'.format(reco_name, reco_space))

        if not isinstance(geometry, Geometry):
            raise TypeError('`geometry` must be a `Geometry` instance, got '
                            '{!r}'.format(geometry))

        # Handle backend choice
        if not _AVAILABLE_IMPLS:
            raise RuntimeError('no ray transform back-end available; '
                               'this requires 3rd party packages, please '
                               'check the install docs')
        impl = kwargs.pop('impl', None)
        if impl is None:
            # Select fastest available
            if ASTRA_CUDA_AVAILABLE:
                impl = 'astra_cuda'
            elif ASTRA_AVAILABLE:
                impl = 'astra_cpu'
                if reco_space.size >= 512 ** 2:
                    warnings.warn(
                        "The best available backend ('astra_cpu') may be too "
                        "slow for volumes of this size. Consider using "
                        "'astra_cuda' if your machine has an Nvidia GPU. "
                        "This warning can be disabled by explicitly setting "
                        "`impl='astra_cpu'`.",
                        RuntimeWarning)
            elif SKIMAGE_AVAILABLE:
                impl = 'skimage'
                if reco_space.size >= 256 ** 2:
                    warnings.warn(
                        "The best available backend ('skimage') may be too "
                        "slow for volumes of this size. Consider using ASTRA. "
                        "This warning can be disabled by explicitly setting "
                        "`impl='skimage'`.",
                        RuntimeWarning)
            else:
                raise RuntimeError('no backend')

        impl, impl_in = str(impl).lower(), impl
        if impl not in _SUPPORTED_IMPL:
            raise ValueError('`impl` {!r} not understood'.format(impl_in))
        if impl not in _AVAILABLE_IMPLS:
            raise ValueError('{!r} back-end not available'.format(impl))

        # Cache for input/output arrays of transforms
        self.use_cache = kwargs.pop('use_cache', True)

        # Sanity checks
        if impl.startswith('astra'):
            if geometry.ndim > 2 and impl.endswith('cpu'):
                raise ValueError('`impl` {!r} only works for 2d'
                                 ''.format(impl_in))

            # Print a warning if the detector midpoint normal vector at any
            # angle is perpendicular to the geometry axis in parallel 3d
            # single-axis geometry -- this is broken in some ASTRA versions
            if (
                isinstance(geometry, Parallel3dAxisGeometry) and
                not astra_supports('par3d_det_mid_pt_perp_to_axis')
            ):
                req_ver = astra_versions_supporting(
                    'par3d_det_mid_pt_perp_to_axis'
                )
                axis = geometry.axis
                mid_pt = geometry.det_params.mid_pt
                for i, angle in enumerate(geometry.angles):
                    if abs(np.dot(axis,
                                  geometry.det_to_src(angle, mid_pt))) < 1e-4:
                        warnings.warn(
                            'angle {}: detector midpoint normal {} is '
                            'perpendicular to the geometry axis {} in '
                            '`Parallel3dAxisGeometry`; this is broken in '
                            'ASTRA {}, please upgrade to ASTRA {}'
                            ''.format(i, geometry.det_to_src(angle, mid_pt),
                                      axis, ASTRA_VERSION, req_ver),
                            RuntimeWarning)
                        break

        elif impl == 'skimage':
            if not isinstance(geometry, Parallel2dGeometry):
                raise TypeError("{!r} backend only supports 2d parallel "
                                'geometries'.format(impl))

            mid_pt = reco_space.domain.mid_pt
            if not np.allclose(mid_pt, [0, 0]):
                raise ValueError('`{}` must be centered at (0, 0), got '
                                 'midpoint {}'.format(reco_name, mid_pt))

            shape = reco_space.shape
            if shape[0] != shape[1]:
                raise ValueError('`{}.shape` must have equal entries, '
                                 'got {}'.format(reco_name, shape))

            extent = reco_space.domain.extent
            if extent[0] != extent[1]:
                raise ValueError('`{}.extent` must have equal entries, '
                                 'got {}'.format(reco_name, extent))

        if reco_space.ndim != geometry.ndim:
            raise ValueError('`{}.ndim` not equal to `geometry.ndim`: '
                             '{} != {}'.format(reco_name, reco_space.ndim,
                                               geometry.ndim))

        self.__geometry = geometry
        self.__impl = impl
        self.__gpu_index = kwargs.pop('gpu_index', 0)

        # Generate or check projection space
        proj_space = kwargs.pop('proj_space', None)
        if proj_space is None:
            dtype = reco_space.dtype
            proj_fspace = FunctionSpace(geometry.params, out_dtype=dtype)

            if not reco_space.is_weighted:
                weighting = None
            elif (isinstance(reco_space.weighting, ConstWeighting) and
                  np.isclose(reco_space.weighting.const,
                             reco_space.cell_volume)):
                # Approximate cell volume
                # TODO: find a way to treat angles and detector differently
                # regarding weighting. While the detector should be uniformly
                # discretized, the angles do not have to and often are not.
                # The needed partition property is available since
                # commit a551190d, but weighting is not adapted yet.
                # See also issue #286
                extent = float(geometry.partition.extent.prod())
                size = float(geometry.partition.size)
                weighting = extent / size
            else:
                raise NotImplementedError('unknown weighting of domain')

            proj_tspace = reco_space.tspace_type(geometry.partition.shape,
                                                 weighting=weighting,
                                                 dtype=dtype)

            if geometry.motion_partition.ndim == 0:
                angle_labels = []
            if geometry.motion_partition.ndim == 1:
                angle_labels = ['$\\varphi$']
            elif geometry.motion_partition.ndim == 2:
                # TODO: check order
                angle_labels = ['$\\vartheta$', '$\\varphi$']
            elif geometry.motion_partition.ndim == 3:
                # TODO: check order
                angle_labels = ['$\\vartheta$', '$\\varphi$', '$\\psi$']
            else:
                angle_labels = None

            if geometry.det_partition.ndim == 1:
                det_labels = ['$s$']
            elif geometry.det_partition.ndim == 2:
                det_labels = ['$u$', '$v$']
            else:
                det_labels = None

            if angle_labels is None or det_labels is None:
                # Fallback for unknown configuration
                axis_labels = None
            else:
                axis_labels = angle_labels + det_labels

            proj_space = DiscreteLp(
                proj_fspace, geometry.partition, proj_tspace,
                axis_labels=axis_labels)

        else:
            # proj_space was given, checking some stuff
            if not isinstance(proj_space, DiscreteLp):
                raise TypeError('`{}` must be a `DiscreteLp` instance, '
                                'got {!r}'.format(proj_name, proj_space))
            if proj_space.shape != geometry.partition.shape:
                raise ValueError('`{}.shape` not equal to `geometry.shape`: '
                                 '{} != {}'.format(proj_name, proj_space.shape,
                                                   geometry.partition.shape))
            if proj_space.dtype != reco_space.dtype:
                raise ValueError('`{}.dtype` not equal to `{}.dtype`: '
                                 '{} != {}'.format(proj_name, reco_name,
                                                   proj_space.dtype,
                                                   reco_space.dtype))

        # Reserve name for cached properties (used for efficiency reasons)
        self._adjoint = None
        self._astra_wrapper = None

        # Extra kwargs that can be reused for adjoint etc. These must
        # be retrieved with `get` instead of `pop` above.
        self._extra_kwargs = kwargs

        # Finally, initialize the Operator structure
        if variant == 'forward':
            super(RayTransformBase, self).__init__(
                domain=reco_space, range=proj_space, linear=True)
        elif variant == 'backward':
            super(RayTransformBase, self).__init__(
                domain=proj_space, range=reco_space, linear=True)

    @property
    def impl(self):
        """Implementation back-end for the evaluation of this operator."""
        return self.__impl

    @property
    def geometry(self):
        """Geometry of this operator."""
        return self.__geometry

    @property
    def gpu_index(self):
        """Index of GPU for ``'astra_cuda'`` implementation."""
        return self.__gpu_index

    @gpu_index.setter
    def gpu_index(self, gpu_index):
        """Set index of GPU for ``'astra_cuda'`` implementation."""
        if not isinstance(gpu_index, int):
            raise TypeError('`gpu_index` must be an integer')
        assert gpu_index >= 0
        if gpu_index != self.__gpu_index:
            # Clear cached properties
            self._adjoint = None
            self._astra_wrapper = None
        self.__gpu_index = gpu_index

    def _call(self, x, out=None):
        """Return ``self(x[, out])``."""
        if self.domain.is_real:
            return self._call_real(x, out, **self._extra_kwargs)

        elif self.domain.is_complex:
            result_parts = [
                self._call_real(
                    x.real, getattr(out, 'real', None), **self._extra_kwargs
                ),
                self._call_real(
                    x.imag, getattr(out, 'imag', None), **self._extra_kwargs
                ),
            ]

            if out is None:
                out = self.range.element()
                out.real = result_parts[0]
                out.imag = result_parts[1]

            return out

        else:
            raise RuntimeError('bad domain {!r}'.format(self.domain))

    def _replicate(self):
        """Return replica that can be configured independently.

        Useful to create multiple instances on different GPUs."""
        replica = copy(self)
        # Clear cached properties
        replica._adjoint = None
        replica._astra_wrapper = None
        return replica


class RayTransform(RayTransformBase):

    """Discrete Ray transform between L^p spaces."""

    def __init__(self, domain, geometry, **kwargs):
        """Initialize a new instance.

        Parameters
        ----------
        domain : `DiscreteLp`
            Discretized reconstruction space, the domain of the forward
            projector.
        geometry : `Geometry`
            Geometry of the transform, containing information about
            the operator range (projection/sinogram space).

        Other Parameters
        ----------------
        impl : {`None`, 'astra_cuda', 'astra_cpu', 'skimage'}, optional
            Implementation back-end for the transform. Supported back-ends:

            - ``'astra_cuda'``: ASTRA toolbox, using CUDA, 2D or 3D
            - ``'astra_cpu'``: ASTRA toolbox using CPU, only 2D
            - ``'skimage'``: scikit-image, only 2D parallel with square
              reconstruction space.

            For the default ``None``, the fastest available back-end is
            used, tried in the above order.
        range : `DiscreteLp`, optional
            Discretized projection (sinogram) space, the range of the
            forward projector.
            Default: Inferred from parameters.
        use_cache : bool, optional
            If ``True``, data is cached. This gives a significant speed-up
            at the expense of a notable memory overhead, both on the GPU
            and on the CPU, since a full volume and a projection dataset
            are stored. That may be prohibitive in 3D.
            Default: True
        gpu_index : int, optional
            Index of GPU to use for ``impl='astra_cuda'``.
            Default: ``0``
        kwargs
            Further keyword arguments passed to the projector backend.

        Notes
        -----
        The ASTRA backend is faster if data are given with
        ``dtype='float32'`` and storage order 'C'. Otherwise copies will be
        needed.

        See Also
        --------
        astra_cpu_forward_projector
        AstraCudaProjectorImpl
        skimage_radon_forward_projector
        """
        range = kwargs.pop('range', None)
        super(RayTransform, self).__init__(
            reco_space=domain, proj_space=range, geometry=geometry,
            variant='forward', **kwargs)

    def _call_real(self, x_real, out_real, **kwargs):
        """Real-space forward projection for the current set-up.

        This method also sets ``self._astra_projector`` for
        ``impl='astra_cuda'`` and enabled cache.
        """
        if self.impl.startswith('astra'):
            backend, data_impl = self.impl.split('_')

            if data_impl == 'cpu':
                return astra_cpu_forward_projector(
                    x_real, self.geometry, self.range.real_space, out_real,
                    **kwargs)

            elif data_impl == 'cuda':
                if self._astra_wrapper is None:
                    astra_wrapper = AstraCudaProjectorImpl(
                        self.geometry, self.domain.real_space,
                        self.range.real_space, gpu_index=self.gpu_index)
                    if self.use_cache:
                        self._astra_wrapper = astra_wrapper
                else:
                    astra_wrapper = self._astra_wrapper

                return astra_wrapper.call_forward(x_real, out_real, **kwargs)
            else:
                # Should never happen
                raise RuntimeError('bad `impl` {!r}'.format(self.impl))

        elif self.impl == 'skimage':
            return skimage_radon_forward_projector(
                x_real, self.geometry, self.range.real_space, out_real,
                **kwargs)
        else:
            # Should never happen
            raise RuntimeError('bad `impl` {!r}'.format(self.impl))

    @property
    def adjoint(self):
        """Adjoint of this operator.

        Returns
        -------
        adjoint : `RayBackProjection`
        """
        if self._adjoint is not None:
            return self._adjoint

        kwargs = self._extra_kwargs.copy()
        kwargs['domain'] = self.range
        kwargs['gpu_index'] = self.gpu_index
        self._adjoint = RayBackProjection(self.domain, self.geometry,
                                          impl=self.impl,
                                          use_cache=self.use_cache,
                                          **kwargs)
        return self._adjoint


class RayBackProjection(RayTransformBase):

    """Adjoint of the discrete Ray transform between L^p spaces."""

    def __init__(self, range, geometry, **kwargs):
        """Initialize a new instance.

        Parameters
        ----------
        range : `DiscreteLp`
            Discretized reconstruction space, the range of the
            backprojection operator.
        geometry : `Geometry`
            Geometry of the transform, containing information about
            the operator domain (projection/sinogram space).

        Other Parameters
        ----------------
        impl : {`None`, 'astra_cuda', 'astra_cpu', 'skimage'}, optional
            Implementation back-end for the transform. Supported back-ends:

            - ``'astra_cuda'``: ASTRA toolbox, using CUDA, 2D or 3D
            - ``'astra_cpu'``: ASTRA toolbox using CPU, only 2D
            - ``'skimage'``: scikit-image, only 2D parallel with square
              reconstruction space.

            For the default ``None``, the fastest available back-end is
            used, tried in the above order.

        domain : `DiscreteLp`, optional
            Discretized projection (sinogram) space, the domain of the
            backprojection operator.
            Default: Inferred from parameters.
        use_cache : bool, optional
            If ``True``, data is cached. This gives a significant speed-up
            at the expense of a notable memory overhead, both on the GPU
            and on the CPU, since a full volume and a projection dataset
            are stored. That may be prohibitive in 3D.
            Default: True
        gpu_index : int, optional
            Index of GPU to use for ``impl='astra_cuda'``.
            Default: ``0``
        kwargs
            Further keyword arguments passed to the projector backend.

        Notes
        -----
        The ASTRA backend is faster if data are given with
        ``dtype='float32'`` and storage order 'C'. Otherwise copies will be
        needed.

        See Also
        --------
        astra_cpu_back_projector
        AstraCudaBackProjectorImpl
        skimage_radon_back_projector
        """
        domain = kwargs.pop('domain', None)
        super(RayBackProjection, self).__init__(
            reco_space=range, proj_space=domain, geometry=geometry,
            variant='backward', **kwargs)

    def _call_real(self, x_real, out_real, **kwargs):
        """Real-space back-projection for the current set-up.

        This method also sets ``self._astra_backprojector`` for
        ``impl='astra_cuda'`` and enabled cache.
        """
        if self.impl.startswith('astra'):
            backend, data_impl = self.impl.split('_')
            if data_impl == 'cpu':
                return astra_cpu_back_projector(
                    x_real, self.geometry, self.range.real_space, out_real,
                    **kwargs)
            elif data_impl == 'cuda':
                if self._astra_wrapper is None:
                    astra_wrapper = AstraCudaBackProjectorImpl(
                        self.geometry, self.range.real_space,
                        self.domain.real_space, gpu_index=self.gpu_index)
                    if self.use_cache:
                        self._astra_wrapper = astra_wrapper
                else:
                    astra_wrapper = self._astra_wrapper

                return astra_wrapper.call_backward(x_real, out_real, **kwargs)
            else:
                # Should never happen
                raise RuntimeError('bad `impl` {!r}'.format(self.impl))

        elif self.impl == 'skimage':
            return skimage_radon_back_projector(
                x_real, self.geometry, self.range.real_space, out_real,
                **kwargs)
        else:
            # Should never happen
            raise RuntimeError('bad `impl` {!r}'.format(self.impl))

    @property
    def adjoint(self):
        """Adjoint of this operator.

        Returns
        -------
        adjoint : `RayTransform`
        """
        if self._adjoint is not None:
            return self._adjoint

        kwargs = self._extra_kwargs.copy()
        kwargs['range'] = self.domain
        kwargs['gpu_index'] = self.gpu_index
        self._adjoint = RayTransform(self.range, self.geometry,
                                     impl=self.impl,
                                     use_cache=self.use_cache,
                                     **kwargs)
        return self._adjoint


if __name__ == '__main__':
    from odl.util.testutils import run_doctests
    run_doctests()
