# coding=utf-8
"""
Create netCDF4 Storage Units and write data to them
"""
from __future__ import absolute_import

import logging
from datetime import datetime

import netCDF4
from osgeo import osr
import yaml

try:
    from yaml import CSafeDumper as SafeDumper
except ImportError:
    from yaml import SafeDumper

from datacube.storage.utils import datetime_to_seconds_since_1970

_LOG = logging.getLogger(__name__)
DATASET_YAML_MAX_SIZE = 30000


def map_measurement_descriptor_parameters(measurement_descriptor):
    """Map measurement descriptor parameters to netcdf variable parameters"""
    md_to_netcdf = {'dtype': 'datatype',
                    'nodata': 'fill_value',
                    'varname': 'varname',
                    'zlib': 'zlib',
                    'complevel': 'complevel',
                    'shuffle': 'shuffle',
                    'fletcher32': 'fletcher32',
                    'contiguous': 'contiguous'}
    params = {ncparam: measurement_descriptor[mdkey]
              for mdkey, ncparam in md_to_netcdf.items() if mdkey in measurement_descriptor}

    if 'varname' not in params:
        raise ValueError("'varname' must be specified in 'measurement_descriptor'", measurement_descriptor)

    return params


def create_netcdf_writer(netcdf_path, geobox):
    if geobox.crs.IsGeographic():
        return GeographicNetCDFWriter(netcdf_path, geobox)
    elif geobox.crs.IsProjected():
        return ProjectedNetCDFWriter(netcdf_path, geobox)
    else:
        raise RuntimeError("Unknown projection")


class NetCDFWriter(object):
    """
    Create NetCDF4 Storage Units, with CF Compliant metadata.

    At the moment compliance depends on what is passed in to this class
    as internal compliance checks are not performed.

    :param str netcdf_path: File path at which to create this NetCDF file
    :param datacube.model.GeoBox geobox: Storage Unit definition
    :param num_times: The number of time values allowed to be stored. Unlimited by default.
    """

    def __init__(self, netcdf_path, geobox):
        netcdf_path = str(netcdf_path)

        self.nco = netCDF4.Dataset(netcdf_path, 'w')

        self.netcdf_path = netcdf_path

        self._create_crs_coords_and_variables(geobox)
        self._set_global_attributes(geobox)

    def __enter__(self):
        return self

    def __exit__(self, *optional_exception_arguments):
        self.close()

    def close(self):
        self.nco.close()

    def create_crs_variable(self, geobox):
        raise NotImplementedError()

    def create_coordinate_variables(self, geobox):
        raise NotImplementedError()

    def _create_crs_coords_and_variables(self, geobox):
        self.validate_crs_arguments(geobox)

        self.create_crs_variable(geobox)
        self.create_coordinate_variables(geobox)

    def validate_crs_arguments(self, geobox):
        pass

    def _set_global_attributes(self, geobox):
        """

        :type geobox: datacube.model.GeoBox
        """
        # ACDD Metadata (Recommended)
        geo_extents = geobox.geographic_extent
        geo_extents.append(geo_extents[0])
        self.nco.geospatial_bounds = "POLYGON((" + ", ".join("{0} {1}".format(*p) for p in geo_extents) + "))"
        self.nco.geospatial_bounds_crs = "EPSG:4326"

        geo_aabb = geobox.geographic_boundingbox
        self.nco.geospatial_lat_min = geo_aabb.bottom
        self.nco.geospatial_lat_max = geo_aabb.top
        self.nco.geospatial_lat_units = "degrees_north"
        self.nco.geospatial_lat_resolution = "{} degrees".format(abs(geobox.affine.e))
        self.nco.geospatial_lon_min = geo_aabb.left
        self.nco.geospatial_lon_max = geo_aabb.right
        self.nco.geospatial_lon_units = "degrees_east"
        self.nco.geospatial_lon_resolution = "{} degrees".format(abs(geobox.affine.a))
        self.nco.date_created = datetime.today().isoformat()
        self.nco.history = "NetCDF-CF file created by agdc-v2 at {:%Y%m%d}.".format(datetime.utcnow())

        # Follow ACDD and CF Conventions
        self.nco.Conventions = 'CF-1.6, ACDD-1.3'

        # Attributes from Dataset. For NCI Reqs MUST contain at least title, summary, source, product_version
        # TODO: global attrs come from somewhere
        # for name, value in geobox.global_attrs.items():
        #     self.nco.setncattr(name, value)

    def add_global_attributes(self, global_attrs):
        for name, value in global_attrs.items():
            self.nco.setncattr(name, value)

    def create_time_values(self, time_values):
        self._create_time_dimension(len(time_values))
        times = self.nco.variables['time']
        for idx, val in enumerate(time_values):
            times[idx] = datetime_to_seconds_since_1970(val)

    def _create_time_dimension(self, time_length):
        """
        Create time dimension
        """
        self.nco.createDimension('time', time_length)
        timeo = self.nco.createVariable('time', 'double', 'time')
        timeo.units = 'seconds since 1970-01-01 00:00:00'
        timeo.standard_name = 'time'
        timeo.long_name = 'Time, unix time-stamp'
        timeo.calendar = 'standard'
        timeo.axis = "T"

    def ensure_variable(self, measurement_descriptor, chunking):
        varname = measurement_descriptor['varname']
        if varname in self.nco.variables:
            # TODO: check that var matches
            return self.nco.variables[varname]
        return self._create_data_variable(measurement_descriptor, chunking=chunking)

    def add_source_metadata(self, time_index, metadata_docs):
        """
        Save YAML metadata documents into the `extra_metadata` variable

        :type time_index: int
        :param metadata_docs: List of metadata docs for this timestamp
        :type metadata_docs: list
        """
        if 'extra_metadata' not in self.nco.variables:
            self._create_extra_metadata_variable()
        yaml_str = yaml.dump_all(metadata_docs, Dumper=SafeDumper)
        self.nco.variables['extra_metadata'][time_index] = netCDF4.stringtoarr(yaml_str, DATASET_YAML_MAX_SIZE)

    def _create_extra_metadata_variable(self):
        self.nco.createDimension('nchar', size=DATASET_YAML_MAX_SIZE)
        extra_metadata_variable = self.nco.createVariable('extra_metadata', 'S1', ('time', 'nchar'))
        extra_metadata_variable.long_name = 'Detailed source dataset information'

    def _create_data_variable(self, measurement_descriptor, chunking):
        params = map_measurement_descriptor_parameters(measurement_descriptor)
        params['dimensions'] = [c[0] for c in chunking]
        params['chunksizes'] = [c[1] for c in chunking]
        data_var = self.nco.createVariable(**params)

        data_var.grid_mapping = 'crs'
        data_var.set_auto_maskandscale(False)

        # Copy extra attributes from the measurement descriptor onto the netcdf variable
        if 'attrs' in measurement_descriptor:
            for name, value in measurement_descriptor['attrs'].items():
                # Unicode or str, that is the netcdf4 question
                data_var.setncattr(str(name), str(value))

                # Everywhere else is str, so this can be too

        units = measurement_descriptor.get('units', '1')
        data_var.units = units

        return data_var


class ProjectedNetCDFWriter(NetCDFWriter):
    def validate_crs_arguments(self, geobox):
        grid_mapping_name = _grid_mapping_name(geobox.crs)

        if grid_mapping_name != 'albers_conic_equal_area':
            raise ValueError('{} CRS is not supported'.format(grid_mapping_name))

    def create_crs_variable(self, geobox):
        crs = geobox.crs
        # http://spatialreference.org/ref/epsg/gda94-australian-albers/html/
        # http://cfconventions.org/Data/cf-conventions/cf-conventions-1.7/build/cf-conventions.html#appendix-grid-mappings
        crs_var = self.nco.createVariable('crs', 'i4')
        crs_var.standard_parallel = (crs.GetProjParm('standard_parallel_1'),
                                     crs.GetProjParm('standard_parallel_2'))
        crs_var.longitude_of_central_meridian = crs.GetProjParm('longitude_of_center')
        crs_var.latitude_of_projection_origin = crs.GetProjParm('latitude_of_center')
        crs_var.false_easting = crs.GetProjParm('false_easting')
        crs_var.false_northing = crs.GetProjParm('false_northing')
        crs_var.grid_mapping_name = _grid_mapping_name(crs)
        crs_var.long_name = crs.GetAttrValue('PROJCS')
        crs_var.spatial_ref = crs.ExportToWkt()  # GDAL variable
        crs_var.GeoTransform = _gdal_geotransform(geobox)  # GDAL variable
        return crs_var

    def create_coordinate_variables(self, geobox):
        self.create_x_y_variables(geobox)
        # self.create_lat_lon_variables(geobox)

    def create_x_y_variables(self, geobox):
        nco = self.nco
        coordinate_labels = geobox.coordinate_labels

        nco.createDimension('x', coordinate_labels['x'].size)
        nco.createDimension('y', coordinate_labels['y'].size)

        xvar = nco.createVariable('x', 'double', 'x')
        xvar.long_name = 'x coordinate of projection'
        xvar.units = geobox.crs.GetAttrValue('UNIT')
        xvar.standard_name = 'projection_x_coordinate'
        xvar.axis = "X"
        xvar[:] = coordinate_labels['x']

        yvar = nco.createVariable('y', 'double', 'y')
        yvar.long_name = 'y coordinate of projection'
        yvar.units = geobox.crs.GetAttrValue('UNIT')
        yvar.standard_name = 'projection_y_coordinate'
        yvar.axis = "Y"
        yvar[:] = coordinate_labels['y']

    def create_lat_lon_variables(self, geobox):
        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        to_wgs84 = osr.CoordinateTransformation(geobox.crs, wgs84)

        lats, lons, _ = zip(*[to_wgs84.TransformPoint(x, y)
                              for y in geobox.ys
                              for x in geobox.xs])

        lats_var = self.nco.createVariable('lat', 'double', ('y', 'x'))
        lats_var.long_name = 'latitude coordinate'
        lats_var.standard_name = 'latitude'
        lats_var.units = 'degrees north'
        lats_var[:] = lats_var

        lons_var = self.nco.createVariable('lon', 'double', ('y', 'x'))
        lons_var.long_name = 'longitude coordinate'
        lons_var.standard_name = 'longitude'
        lons_var.units = 'degrees east'
        lons_var[:] = lons_var


class GeographicNetCDFWriter(NetCDFWriter):
    def create_crs_variable(self, geobox):
        crs = geobox.crs
        crs_var = self.nco.createVariable('crs', 'i4')
        crs_var.long_name = crs.GetAttrValue('GEOGCS')  # "Lon/Lat Coords in WGS84"
        crs_var.grid_mapping_name = _grid_mapping_name(crs)
        crs_var.longitude_of_prime_meridian = 0.0
        crs_var.semi_major_axis = crs.GetSemiMajor()
        crs_var.inverse_flattening = crs.GetInvFlattening()
        crs_var.spatial_ref = crs.ExportToWkt()  # GDAL variable
        crs_var.GeoTransform = _gdal_geotransform(geobox)  # GDAL variable
        return crs_var

    def create_coordinate_variables(self, geobox):
        coordinate_labels = geobox.coordinate_labels

        self.nco.createDimension('longitude', coordinate_labels['longitude'].size)
        self.nco.createDimension('latitude', coordinate_labels['latitude'].size)

        lon = self.nco.createVariable('longitude', 'double', 'longitude')
        lon.units = 'degrees_east'
        lon.standard_name = 'longitude'
        lon.long_name = 'longitude'
        lon.axis = "X"
        lon[:] = coordinate_labels['longitude']

        lat = self.nco.createVariable('latitude', 'double', 'latitude')
        lat.units = 'degrees_north'
        lat.standard_name = 'latitude'
        lat.long_name = 'latitude'
        lat.axis = "Y"
        lat[:] = coordinate_labels['latitude']


def _gdal_geotransform(geobox):
    return geobox.affine.to_gdal()


def _grid_mapping_name(crs):
    if crs.IsGeographic():
        return 'latitude_longitude'
    elif crs.IsProjected():
        return crs.GetAttrValue('PROJECTION').lower()
