"""
Methods for reading ROMS model outputs
"""
from thetis import *
from atm_forcing import to_latlon
from thetis.timezone import *
import netCDF4
import scipy.spatial.qhull as qhull
from thetis.interpolation import GridInterpolator, SpatialInterpolator


class SpatialInterpolatorROMS3d(SpatialInterpolator):
    """
    Abstract spatial interpolator class that can interpolate onto a Function
    """
    def __init__(self, function_space, to_latlon):
        """
        :arg function_space: target Firedrake FunctionSpace
        :arg to_latlon: Python function that converts local mesh coordinates to
            latitude and longitude: 'lat, lon = to_latlon(x, y)'
        """
        self.function_space = function_space

        # construct local coordinates
        xyz = SpatialCoordinate(self.function_space.mesh())
        tmp_func = self.function_space.get_work_function()
        xyz_array = np.zeros((tmp_func.dat.data_with_halos.shape[0], 3))
        for i in range(3):
            tmp_func.interpolate(xyz[i])
            xyz_array[:, i] = tmp_func.dat.data_with_halos[:]
        self.function_space.restore_work_function(tmp_func)

        self.latlonz_array = np.zeros_like(xyz_array)
        lat, lon = to_latlon(xyz_array[:, 0], xyz_array[:, 1])
        self.latlonz_array[:, 0] = lat
        self.latlonz_array[:, 1] = lon
        self.latlonz_array[:, 2] = xyz_array[:, 2]

        self._initialized = False

    def _get_subset_nodes(self, grid_x, grid_y, target_x, target_y):
        """
        Retuns grid nodes that are necessary for intepolating onto target_x,y
        """
        orig_shape = grid_x.shape
        grid_xy = np.array((grid_x.ravel(), grid_y.ravel())).T
        target_xy = np.array((target_x.ravel(), target_y.ravel())).T
        tri = qhull.Delaunay(grid_xy)
        simplex = tri.find_simplex(target_xy)
        vertices = np.take(tri.simplices, simplex, axis=0)
        nodes = np.unique(vertices.ravel())
        nodes_x, nodes_y = np.unravel_index(nodes, orig_shape)

        # TODO almost the same as 2D version, refactorize and add slice option
        return nodes, nodes_x, nodes_y

    def _compute_roms_z_coord(self, ncfile, constant_zeta=None):
        zeta = ncfile['zeta'][0, :, :]
        bath = ncfile['h'][:]
        # NOTE compute z coordinates for full levels (w)
        cs = ncfile['Cs_w'][:]
        s = ncfile['s_w'][:]
        hc = ncfile['hc'][:]

        # ROMS transformation ver. 2:
        # z(x, y, sigma, t) = zeta(x, y, t) + (zeta(x, y, t) +  h(x, y))*S(x, y, sigma)
        zeta = zeta[self.ind_lat, self.ind_lon][self.mask].filled(0.0)
        bath = bath[self.ind_lat, self.ind_lon][self.mask]
        if constant_zeta:
            zeta = np.ones_like(bath)*constant_zeta
        ss = (hc*s[:, np.newaxis] + bath[np.newaxis, :]*cs[:, np.newaxis])/(hc + bath[np.newaxis, :])
        grid_z_w = zeta[np.newaxis, :]*(1 + ss) + bath[np.newaxis, :]*ss
        grid_z = 0.5*(grid_z_w[1:, :] + grid_z_w[:-1, :])
        grid_z[0, :] = grid_z_w[0, :]
        grid_z[-1, :] = grid_z_w[-1, :]
        return grid_z

    def _create_interpolator(self, ncfile):
        """
        Create compact interpolator by finding the minimal necessary support
        """
        lat = ncfile['lat_rho'][:]
        lon = ncfile['lon_rho'][:]
        self.mask = ncfile['mask_rho'][:].astype(bool)
        self.nodes, self.ind_lat, self.ind_lon = self._get_subset_nodes(lat, lon, self.latlonz_array[:, 0], self.latlonz_array[:, 1])
        lat_subset = lat[self.ind_lat, self.ind_lon]
        lon_subset = lon[self.ind_lat, self.ind_lon]
        self.mask = self.mask[self.ind_lat, self.ind_lon]

        # COMPUTE z coords for constant elevation=0.1
        grid_z = self._compute_roms_z_coord(ncfile, constant_zeta=0.1)

        # omit land mask
        lat_subset = lat_subset[self.mask]
        lon_subset = lon_subset[self.mask]

        nz = grid_z.shape[0]

        # data shape is [nz, neta, nxi]
        grid_lat = np.tile(lat_subset, (nz, 1, 1)).ravel()
        grid_lon = np.tile(lon_subset, (nz, 1, 1)).ravel()
        grid_z = grid_z.ravel()
        grid_latlonz = np.vstack((grid_lat, grid_lon, grid_z)).T

        # building 3D interpolator, this can take a long time (minutes)
        print_output('Constructing 3D GridInterpolator...')
        self.interpolator = GridInterpolator(grid_latlonz, self.latlonz_array, normalize=True,
                                             fill_mode='nearest')
        print_output('done.')

        self._initialized = True

    def interpolate(self, nc_filename, variable_list, itime):
        """
        Calls the interpolator object
        """
        with netCDF4.Dataset(nc_filename, 'r') as ncfile:
            if not self._initialized:
                self._create_interpolator(ncfile)
            output = []
            for var in variable_list:
                assert var in ncfile.variables
                # TODO generalize data dimensions, sniff from netcdf file
                grid_data = ncfile[var][itime, :, :, :][:, self.ind_lat, self.ind_lon][:, self.mask].filled(np.nan).ravel()
                data = self.interpolator(grid_data)
                output.append(data)
        return output


class LiveOceanInterpolator(object):
    """
    Interpolates LiveOcean (ROMS) model data on 3D fields
    """
    def __init__(self, function_space, fields, field_names, ncfile_pattern, init_date):
        self.function_space = function_space
        for f in fields:
            assert f.function_space() == self.function_space, 'field \'{:}\' does not belong to given function space {:}.'.format(f.name(), self.function_space.name)
        assert len(fields) == len(field_names)
        self.fields = fields
        self.field_names = field_names

        # construct interpolators
        self.grid_interpolator = SpatialInterpolatorROMS3d(self.function_space, to_latlon)
        self.reader = interpolation.NetCDFSpatialInterpolator(self.grid_interpolator, field_names)
        self.timesearch_obj = interpolation.NetCDFTimeSearch(ncfile_pattern, init_date, interpolation.NetCDFTimeParser, time_variable_name='ocean_time', verbose=False)
        self.time_interpolator = interpolation.LinearTimeInterpolator(self.timesearch_obj, self.reader)

    def set_fields(self, time):
        """
        Evaluates forcing fields at the given time
        """
        vals = self.time_interpolator(time)
        for i in range(len(self.fields)):
            self.fields[i].dat.data_with_halos[:] = vals[i]


def test():
    # test time parser
    tp = interpolation.NetCDFTimeParser('forcings/liveocean/f2015.05.16/ocean_his_0002.nc', time_variable_name='ocean_time')
    nc_start = datetime.datetime(2015, 5, 16, 1, tzinfo=pytz.utc)
    assert tp.start_time == nc_start
    assert tp.end_time == nc_start
    assert np.allclose(tp.time_array, np.array([datetime_to_epoch(nc_start)]))

    # test time search
    ncpattern = 'forcings/liveocean/f2015.*/ocean_his_*.nc'
    timesearch_obj = interpolation.NetCDFTimeSearch(ncpattern, init_date, interpolation.NetCDFTimeParser, time_variable_name='ocean_time', verbose=True)

    sim_time = 100.0
    fn, itime, time = timesearch_obj.find(sim_time, previous=True)
    assert fn == 'forcings/liveocean/f2015.05.16/ocean_his_0009.nc'
    assert itime == 0
    assert time == 0.0
    fn, itime, time = timesearch_obj.find(sim_time)
    assert fn == 'forcings/liveocean/f2015.05.16/ocean_his_0010.nc'
    assert itime == 0
    assert time == 3600.0

    dt = 10.465116
    for i in range(100):
        print(i)
        fn, itime, time = timesearch_obj.find(i*dt, previous=True)
        print('  {:} {:}'.format(fn, itime))
        fn, itime, time = timesearch_obj.find(i*dt, previous=False)
        print('  {:} {:}'.format(fn, itime))

    # test full interpolator
    interp = LiveOceanInterpolator(p1,
                                   [salt, temp],
                                   ['salt', 'temp'],
                                   'forcings/liveocean/f2015.*/ocean_his_*.nc',
                                   init_date)
    interp.set_fields(0.0)
    out_salt = File('tmp/salt.pvd')
    out_temp = File('tmp/temp.pvd')

    out_salt.write(salt)
    out_temp.write(temp)

    dt = 900.
    for i in range(20):
        interp.set_fields((i+1)*dt)
        out_salt.write(salt)
        out_temp.write(temp)


if __name__ == '__main__':
    test()
