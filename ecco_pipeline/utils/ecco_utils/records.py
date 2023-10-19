import xarray as xr
import numpy as np
from pathlib import Path
from utils.ecco_utils.llc_array_conversion import llc_tiles_to_compact, aste_stacked_to_compact


def make_empty_record(record_date: str, model_grid: xr.Dataset, array_precision: type) -> xr.DataArray:
    '''
    Creates xarray DataArray filled with nans.
    '''
    # model_grid must contain the corrdinates XC and YC

    # make an empty data array to hold the interpolated 2D field
    # all values are nans.
    # dimensions are the same as model_grid.XC
    nan_array = np.full(model_grid.XC.values.shape, np.nan, array_precision)
    data_DA = xr.DataArray(nan_array, dims=model_grid.XC.dims)

    data_DA = data_DA.assign_coords(time=np.datetime64(record_date, 'ns'))
    data_DA = data_DA.expand_dims(dim='time', axis=0)

    # add start and end time records. default is same value as record date
    data_DA = data_DA.assign_coords({'time_start': ('time', data_DA.time.data.copy()),
                                     'time_end': ('time', data_DA.time.data.copy())})

    for dim in model_grid.XC.dims:
        data_DA = data_DA.assign_coords({dim: model_grid[dim]})

    try:
        data_DA = data_DA.assign_coords({'XC': (model_grid.XC.dims, model_grid.XC.data),
                                        'YC': (model_grid.YC.dims, model_grid.YC.data)})
    except:
        print('Unsupported model grid format')
        return []

    data_DA.XC.attrs['coverage_content_type'] = 'coordinate'
    data_DA.YC.attrs['coverage_content_type'] = 'coordinate'

    # copy over the attributes from XC and YC to the dataArray
    data_DA.XC.attrs = model_grid.XC.attrs
    data_DA.YC.attrs = model_grid.YC.attrs

    data_DA.name = 'Default empty model grid record'

    return data_DA


def save_binary(data, output_filename, binary_fill_value, binary_output_dir, binary_output_dtype,
                model_grid_type, data_var=''):
    if data_var:
        data_values = data[data_var].values
    else:
        data_values = data.values

    # define binary file output filetype
    dt_out = np.dtype(binary_output_dtype)

    # create directory
    binary_output_dir.mkdir(exist_ok=True)

    # define binary output filename
    binary_output_filename = binary_output_dir / output_filename

    # replace nans with the binary fill value (something like -9999)
    tmp_fields = np.where(np.isnan(data_values),
                          binary_fill_value, data_values)

    # SAVE FLAT BINARY
    # loop through each record of the year, save binary fields one at a time
    # appending each record as we go
    fd1 = open(str(binary_output_filename), 'wb')
    fd1 = open(str(binary_output_filename), 'ab')

    for i in range(len(data.time)):
        # print('saving binary record: ', str(i))

        # if we have an llc grid, then we have to reform to compact
        if model_grid_type == 'llc':
            tmp_field = llc_tiles_to_compact(tmp_fields[i, :], less_output=True)

        # otherwise assume grid is x,y (2 dimensions)
        elif model_grid_type == 'latlon':
            tmp_field = tmp_fields[i, :]
            
        elif model_grid_type == 'aste_llc':
            tmp_field = aste_stacked_to_compact(tmp_fields[i, :], less_output=True)

        else:
            print('unknown model grid type!')
            tmp_field = []
            return []

        # make sure we have something to save...
        if len(tmp_field) > 0:
            # if this is the first record, create new binary file
            tmp_field.astype(dt_out).tofile(fd1)

    # close the file at the end of the operation
    fd1.close()


def save_netcdf(data, output_filename, netcdf_fill_value, netcdf_output_dir):
    # create directory
    netcdf_output_dir.mkdir(exist_ok=True)

    # define netcdf output filename
    netcdf_output_filename = netcdf_output_dir / Path(output_filename + '.nc')

    try:
        data = data.fillna(netcdf_fill_value)
        data_DS = data.to_dataset()
    except:
        data_DS = data

    coord_encoding = {}
    for coord in data_DS.coords:
        coord_encoding[coord] = {'_FillValue': None, 'dtype': 'float32'}

        if coord == 'time' or coord == 'time_bnds':
            coord_encoding[coord] = {'dtype': 'int32'}
        coord_encoding['time'] = {'units': 'hours since 1980-01-01'}

    var_encoding = {}
    for var in data_DS.data_vars:
        var_encoding[var] = {'zlib': True,
                             'complevel': 5,
                             'shuffle': True,
                             '_FillValue': netcdf_fill_value}

    encoding = {**coord_encoding, **var_encoding}
    # the actual saving (so easy with xarray!)
    data_DS.to_netcdf(netcdf_output_filename,  encoding=encoding)
    data_DS.close()


# def save_to_disk(data, output_filename, binary_fill_value, netcdf_fill_value,
#                  netcdf_output_dir, binary_output_dir, binary_output_dtype,
#                  model_grid_type, save_binary=True, save_netcdf=True, data_var=''):

#     if save_binary:
#         if data_var:
#             data_values = data[data_var].values
#         else:
#             data_values = data.values

#         # define binary file output filetype
#         dt_out = np.dtype(binary_output_dtype)

#         # create directory
#         binary_output_dir.mkdir(exist_ok=True)

#         # define binary output filename
#         binary_output_filename = binary_output_dir / output_filename

#         # replace nans with the binary fill value (something like -9999)
#         tmp_fields = np.where(np.isnan(data_values),
#                               binary_fill_value, data_values)

#         # SAVE FLAT BINARY
#         # loop through each record of the year, save binary fields one at a time
#         # appending each record as we go
#         fd1 = open(str(binary_output_filename), 'wb')
#         fd1 = open(str(binary_output_filename), 'ab')

#         for i in range(len(data.time)):
#             # print('saving binary record: ', str(i))

#             # if we have an llc grid, then we have to reform to compact
#             if model_grid_type == 'llc':
#                 tmp_field = llc_tiles_to_compact(
#                     tmp_fields[i, :], less_output=True)

#             # otherwise assume grid is x,y (2 dimensions)
#             elif model_grid_type == 'latlon':
#                 tmp_field = tmp_fields[i, :]

#             else:
#                 print('unknown model grid type!')
#                 tmp_field = []
#                 return []

#             # make sure we have something to save...
#             if len(tmp_field) > 0:
#                 # if this is the first record, create new binary file
#                 tmp_field.astype(dt_out).tofile(fd1)

#         # close the file at the end of the operation
#         fd1.close()

#     if save_netcdf:
#         # print('saving netcdf record')

#         # create directory
#         netcdf_output_dir.mkdir(exist_ok=True)

#         # define netcdf output filename
#         netcdf_output_filename = netcdf_output_dir / Path(output_filename + '.nc')

#         # SAVE NETCDF
#         # replace the binary fill value (-9999) with the netcdf fill value
#         # which is much more interesting

#         # replace nans with the binary fill value (something like -9999) if
#         # the xarray object sent in a datarray that hasnt been checked
#         try:
#             data = data.fillna(netcdf_fill_value)
#             data_DS = data.to_dataset()
#         except:
#             data_DS = data

#         coord_encoding = {}
#         for coord in data_DS.coords:
#             coord_encoding[coord] = {'_FillValue': None, 'dtype': 'float32'}

#             if coord == 'time' or coord == 'time_bnds':
#                 coord_encoding[coord] = {'dtype': 'int32',
#                                          'units': 'seconds since 1980-01-01T00:00:00'}

#         var_encoding = {}
#         for var in data_DS.data_vars:
#             var_encoding[var] = {'zlib': True,
#                                  'complevel': 5,
#                                  'shuffle': True,
#                                  '_FillValue': netcdf_fill_value}

#         encoding = {**coord_encoding, **var_encoding}
        
        
#         encoding_each = {'zlib': True,
#                          'complevel': 5,
#                          'shuffle': True,
#                          '_FillValue': netcdf_fill_value}

#         coord_encoding = {}
#         for coord in data_DS.coords:
#             coord_encoding[coord] = {'_FillValue': None}

#             if 'time' in coord:
#                 coord_encoding[coord] = {'_FillValue': None,
#                                          'dtype': 'int32'}
#                 if coord != 'time_step':
#                     coord_encoding[coord]['units'] = "hours since 1992-01-01 12:00:00"
#             if 'lat' in coord:
#                 coord_encoding[coord] = {'_FillValue': None,
#                                          'dtype': 'float32'}
#             if 'lon' in coord:
#                 coord_encoding[coord] = {'_FillValue': None,
#                                          'dtype': 'float32'}
#             if 'Z' in coord:
#                 coord_encoding[coord] = {'_FillValue': None,
#                                          'dtype': 'float32'}

#         var_encoding = {var: encoding_each for var in data_DS.data_vars}

#         encoding = {**coord_encoding, **var_encoding}
#         # the actual saving (so easy with xarray!)
#         data_DS.to_netcdf(netcdf_output_filename,  encoding=encoding)
#         data_DS.close()
