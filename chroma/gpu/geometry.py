import numpy as np
import pycuda.driver as cuda
from pycuda import gpuarray as ga
from pycuda import characterize

from chroma.geometry import standard_wavelengths
from chroma.gpu.tools import get_cu_module, get_cu_source, cuda_options, \
    chunk_iterator, format_array, format_size, to_uint3, to_float3, \
    make_gpu_struct
from chroma.log import logger

class GPUGeometry(object):
    def __init__(self, geometry, wavelengths=None, print_usage=False):
        if wavelengths is None:
            wavelengths = standard_wavelengths

        try:
            wavelength_step = np.unique(np.diff(wavelengths)).item()
        except ValueError:
            raise ValueError('wavelengths must be equally spaced apart.')

        geometry_source = get_cu_source('geometry.h')
        material_struct_size = characterize.sizeof('Material', geometry_source)
        surface_struct_size = characterize.sizeof('Surface', geometry_source)
        geometry_struct_size = characterize.sizeof('Geometry', geometry_source)

        self.material_data = []
        self.material_ptrs = []

        def interp_material_property(wavelengths, property):
            # note that it is essential that the material properties be
            # interpolated linearly. this fact is used in the propagation
            # code to guarantee that probabilities still sum to one.
            return np.interp(wavelengths, property[:,0], property[:,1]).astype(np.float32)

        for i in range(len(geometry.unique_materials)):
            material = geometry.unique_materials[i]

            if material is None:
                raise Exception('one or more triangles is missing a material.')

            refractive_index = interp_material_property(wavelengths, material.refractive_index)
            refractive_index_gpu = ga.to_gpu(refractive_index)
            absorption_length = interp_material_property(wavelengths, material.absorption_length)
            absorption_length_gpu = ga.to_gpu(absorption_length)
            scattering_length = interp_material_property(wavelengths, material.scattering_length)
            scattering_length_gpu = ga.to_gpu(scattering_length)

            self.material_data.append(refractive_index_gpu)
            self.material_data.append(absorption_length_gpu)
            self.material_data.append(scattering_length_gpu)

            material_gpu = \
                make_gpu_struct(material_struct_size,
                                [refractive_index_gpu, absorption_length_gpu,
                                 scattering_length_gpu,
                                 np.uint32(len(wavelengths)),
                                 np.float32(wavelength_step),
                                 np.float32(wavelengths[0])])

            self.material_ptrs.append(material_gpu)

        self.material_pointer_array = \
            make_gpu_struct(8*len(self.material_ptrs), self.material_ptrs)

        self.surface_data = []
        self.surface_ptrs = []

        for i in range(len(geometry.unique_surfaces)):
            surface = geometry.unique_surfaces[i]

            if surface is None:
                # need something to copy to the surface array struct
                # that is the same size as a 64-bit pointer.
                # this pointer will never be used by the simulation.
                self.surface_ptrs.append(np.uint64(0))
                continue

            detect = interp_material_property(wavelengths, surface.detect)
            detect_gpu = ga.to_gpu(detect)
            absorb = interp_material_property(wavelengths, surface.absorb)
            absorb_gpu = ga.to_gpu(absorb)
            reflect_diffuse = interp_material_property(wavelengths, surface.reflect_diffuse)
            reflect_diffuse_gpu = ga.to_gpu(reflect_diffuse)
            reflect_specular = interp_material_property(wavelengths, surface.reflect_specular)
            reflect_specular_gpu = ga.to_gpu(reflect_specular)

            self.surface_data.append(detect_gpu)
            self.surface_data.append(absorb_gpu)
            self.surface_data.append(reflect_diffuse_gpu)
            self.surface_data.append(reflect_specular_gpu)

            surface_gpu = \
                make_gpu_struct(surface_struct_size,
                                [detect_gpu, absorb_gpu,
                                 reflect_diffuse_gpu,
                                 reflect_specular_gpu,
                                 np.uint32(len(wavelengths)),
                                 np.float32(wavelength_step),
                                 np.float32(wavelengths[0])])

            self.surface_ptrs.append(surface_gpu)

        self.surface_pointer_array = \
            make_gpu_struct(8*len(self.surface_ptrs), self.surface_ptrs)

        self.vertices = ga.to_gpu(to_float3(geometry.mesh.vertices))
        self.triangles = ga.to_gpu(to_uint3(geometry.mesh.triangles))

        material_codes = (((geometry.material1_index & 0xff) << 24) |
                          ((geometry.material2_index & 0xff) << 16) |
                          ((geometry.surface_index & 0xff) << 8)).astype(np.uint32)

        self.material_codes = ga.to_gpu(material_codes)

        self.lower_bounds = ga.to_gpu(to_float3(geometry.lower_bounds))
        self.upper_bounds = ga.to_gpu(to_float3(geometry.upper_bounds))
        self.colors = ga.to_gpu(geometry.colors.astype(np.uint32))
        self.node_map = ga.to_gpu(geometry.node_map.astype(np.uint32))
        self.node_map_end = ga.to_gpu(geometry.node_map_end.astype(np.uint32))
        self.solid_id_map = ga.to_gpu(geometry.solid_id.astype(np.uint32))

        self.gpudata = make_gpu_struct(geometry_struct_size,
                                       [self.vertices, self.triangles,
                                        self.material_codes,
                                        self.colors, self.lower_bounds,
                                        self.upper_bounds, self.node_map,
                                        self.node_map_end,
                                        self.material_pointer_array,
                                        self.surface_pointer_array,
                                        np.uint32(geometry.start_node),
                                        np.uint32(geometry.first_node)])

        self.geometry = geometry

        if print_usage:
            self.print_device_usage()
        logger.info(self.device_usage_str())

    def device_usage_str(self):
        '''Returns a formatted string displaying the memory usage.'''
        s = 'device usage:\n'
        s += '-'*10 + '\n'
        s += format_array('vertices', self.vertices) + '\n'
        s += format_array('triangles', self.triangles) + '\n'
        s += format_array('lower_bounds', self.lower_bounds) + '\n'
        s += format_array('upper_bounds', self.upper_bounds) + '\n'
        s += format_array('node_map', self.node_map) + '\n'
        s += format_array('node_map_end', self.node_map_end) + '\n'
        s += '%-15s %6s %6s' % ('total', '', format_size(self.vertices.nbytes + self.triangles.nbytes + self.lower_bounds.nbytes + self.upper_bounds.nbytes + self.node_map.nbytes + self.node_map_end.nbytes)) + '\n'
        s += '-'*10 + '\n'
        free, total = cuda.mem_get_info()
        s += '%-15s %6s %6s' % ('device total', '', format_size(total)) + '\n'
        s += '%-15s %6s %6s' % ('device used', '', format_size(total-free)) + '\n'
        s += '%-15s %6s %6s' % ('device free', '', format_size(free)) + '\n'
        return s

    def print_device_usage(self):
        print self.device_usage_str()
        print 

    def reset_colors(self):
        self.colors.set_async(self.geometry.colors.astype(np.uint32))

    def color_solids(self, solid_hit, colors, nblocks_per_thread=64,
                     max_blocks=1024):
        solid_hit_gpu = ga.to_gpu(np.array(solid_hit, dtype=np.bool))
        solid_colors_gpu = ga.to_gpu(np.array(colors, dtype=np.uint32))

        module = get_cu_module('mesh.h', options=cuda_options)
        color_solids = module.get_function('color_solids')

        for first_triangle, triangles_this_round, blocks in \
                chunk_iterator(self.triangles.size, nblocks_per_thread,
                               max_blocks):
            color_solids(np.int32(first_triangle),
                         np.int32(triangles_this_round), self.solid_id_map,
                         solid_hit_gpu, solid_colors_gpu, self.gpudata,
                         block=(nblocks_per_thread,1,1), 
                         grid=(blocks,1))
