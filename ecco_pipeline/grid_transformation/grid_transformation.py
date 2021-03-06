import logging
import logging.config
import os
import pickle
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pyresample as pr
import xarray as xr
import yaml
from netCDF4 import default_fillvals  # pylint: disable=no-name-in-module
from utils import file_utils, solr_utils

np.warnings.filterwarnings('ignore')

logging.config.fileConfig('logs/log.ini', disable_existing_loggers=False)
log = logging.getLogger(__name__)

try:
    sys.path.append(str(Path('../ecco-cloud-utils/').resolve()))
    import ecco_cloud_utils as ea  # pylint: disable=import-error
except Exception as e:
    log.exception(e)


def transformation(source_file_path, remaining_transformations, output_dir, config, verbose=True):
    """
    Performs and saves locally all remaining transformations for a given source granule
    Updates Solr with transformation entries and updates descendants, and dataset entries
    """

    # Conditional function definition:
    # verboseprint will use the print function if verbose is true
    # otherwise will use a lambda function that returns None (effectively not printing)
    verboseprint = print if verbose else lambda *a, **k: None

    # Define precision of output files, float32 is standard
    array_precision = getattr(np, config['array_precision'])

    # Define fill values for binary and netcdf
    if array_precision == np.float32:
        binary_dtype = '>f4'
        netcdf_fill_value = default_fillvals['f4']

    elif array_precision == np.float64:
        binary_dtype = '>f8'
        netcdf_fill_value = default_fillvals['f8']

    fill_values = {'binary': -9999, 'netcdf': netcdf_fill_value}

    # =====================================================
    # Set configuration options
    # =====================================================
    file_name = source_file_path.split('/')[-1]
    dataset_name = config['ds_name']
    transformation_version = config['t_version']

    # Query Solr for dataset entry
    fq = [f'dataset_s:{dataset_name}', 'type_s:dataset']
    dataset_metadata = solr_utils.solr_query(fq)[0]

    # Query Solr for harvested entry to get origin_checksum and date
    query_fq = [f'dataset_s:{dataset_name}', 'type_s:granule',
                f'pre_transformation_file_path_s:"{source_file_path}"']
    harvested_metadata = solr_utils.solr_query(query_fq)[0]
    origin_checksum = harvested_metadata['checksum_s']
    date = harvested_metadata['date_s']

    # If data is stored in hemispheres, use that hemisphere when naming files and updating Solr
    # Otherwise, leave it blank
    # While using hemi in naming files, care has been taken to ensure proper filenames are created when working with both types
    if 'hemisphere_s' in harvested_metadata.keys():
        hemi = f'_{harvested_metadata["hemisphere_s"]}'
    else:
        hemi = ''

    transformation_successes = True
    transformation_file_paths = {}

    grids_updated = []

    # =====================================================
    # Load file to transform
    # =====================================================
    verboseprint(f'\n====== Loading {file_name} data =======\n')

    ds = xr.open_dataset(source_file_path, decode_times=True)
    ds.attrs['original_file_name'] = file_name

    # Iterate through grids in remaining_transformations
    for grid_name in remaining_transformations.keys():
        fields = remaining_transformations[grid_name]

        # Query Solr for grid metadata
        fq = ['type_s:grid', f'grid_name_s:{grid_name}']
        grid_metadata = solr_utils.solr_query(fq)[0]

        grid_path = grid_metadata['grid_path_s']
        grid_type = grid_metadata['grid_type_s']

        # =====================================================
        # Load grid
        # =====================================================
        verboseprint(f' - Loading {grid_name} model grid')
        model_grid = xr.open_dataset(grid_path).reset_coords()

        # =====================================================
        # Make model grid factors if not present locally
        # =====================================================
        grid_factors = f'{grid_name}{hemi}_factors_path_s'
        grid_factors_version = f'{grid_name}{hemi}_factors_version_f'

        # check to see if there is 'grid_factors_version' key in the
        # dataset and whether the transformation version matches with the
        # current version
        if grid_factors_version in dataset_metadata.keys() and \
                transformation_version == dataset_metadata[grid_factors_version]:

            factors_path = dataset_metadata[grid_factors]

            verboseprint(f' - Loading {grid_name} factors')
            with open(factors_path, "rb") as f:
                factors = pickle.load(f)

        else:
            verboseprint(f' - Creating {grid_name} factors')

            fq = [f'dataset_s:{dataset_name}', 'type_s:dataset']
            short_name = dataset_metadata['short_name_s']

            data_res = config['data_res']

            # If data_res is fractional, convert from string to float
            if type(data_res) is str and '/' in data_res:
                num, den = data_res.replace(' ', '').split('/')
                data_res = float(num) / float(den)

            # Use hemisphere specific variables if data is hemisphere specific
            if hemi:
                hemi_dim = config[f'dims{hemi}']
                hemi_area_extent = config[f'area_extent{hemi}']
                hemi_proj_info = config[f'proj_info{hemi}']
                hemi_data_max_lat = config[f'data_max_lat{hemi}']

                source_grid_min_L, source_grid_max_L, source_grid, \
                    data_grid_lons, data_grid_lats = ea.generalized_grid_product(short_name,
                                                                                 data_res,
                                                                                 hemi_data_max_lat,
                                                                                 hemi_area_extent,
                                                                                 hemi_dim,
                                                                                 hemi_proj_info)
            else:
                source_grid_min_L, source_grid_max_L, source_grid, \
                    data_grid_lons, data_grid_lats = ea.generalized_grid_product(short_name,
                                                                                 data_res,
                                                                                 config['data_max_lat'],
                                                                                 config['area_extent'],
                                                                                 config['dims'],
                                                                                 config['proj_info'])

            # Define the 'swath' as the lats/lon pairs of the model grid
            target_grid = pr.geometry.SwathDefinition(lons=model_grid.XC.values.ravel(),
                                                      lats=model_grid.YC.values.ravel())

            # Retrieve target_grid_radius from model_grid file
            if 'effective_grid_radius' in model_grid:
                target_grid_radius = model_grid.effective_grid_radius.values.ravel()
            elif 'effective_radius' in model_grid:
                target_grid_radius = model_grid.effective_radius.values.ravel()
            elif 'RAD' in model_grid:
                target_grid_radius = model_grid.RAD.values.ravel()
            elif 'rA' in model_grid:
                target_grid_radius = 0.5*np.sqrt(model_grid.rA.values.ravel())
            else:
                print(f'ERROR - {grid_name} grid not supported')
                continue

            # Compute the mapping between the data and model grid
            source_indices_within_target_radius_i,\
                num_source_indices_within_target_radius_i,\
                nearest_source_index_to_target_index_i = \
                ea.find_mappings_from_source_to_target(source_grid,
                                                       target_grid,
                                                       target_grid_radius,
                                                       source_grid_min_L,
                                                       source_grid_max_L)

            factors = (source_indices_within_target_radius_i,
                       num_source_indices_within_target_radius_i,
                       nearest_source_index_to_target_index_i)

            verboseprint(f' - Saving {grid_name} factors')
            factors_path = f'{output_dir}/{dataset_name}/transformed_products/{grid_name}/'

            # Create directory if needed and save factors
            if not os.path.exists(factors_path):
                os.makedirs(factors_path)

            factors_path += f'{grid_name}{hemi}_factors'

            with open(factors_path, 'wb') as f:
                pickle.dump(factors, f)

            verboseprint(' - Updating Solr with factors')
            # Query Solr for dataset entry
            query_fq = [f'dataset_s:{dataset_name}', 'type_s:dataset']
            dataset_metadata_id = solr_utils.solr_query(query_fq)[0]['id']

            # Update Solr dataset entry with factors metadata
            update_body = [
                {
                    "id": dataset_metadata_id,
                    f'{grid_factors}': {"set": factors_path},
                    f'{grid_name}{hemi}_factors_stored_dt': {"set": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
                    f'{grid_factors_version}': {"set": transformation_version}
                }
            ]

            r = solr_utils.solr_update(update_body, r=True)

            if r.status_code == 200:
                verboseprint(
                    '    - Successfully updated Solr with factors information')
            else:
                verboseprint(
                    '    - Failed to update Solr with factors information')

        update_body = []

        # Iterate through remaining transformation fields
        for field in fields:
            field_name = field["name_s"]

            # Query if grid/field combination transformation entry exists
            query_fq = [f'dataset_s:{dataset_name}', 'type_s:transformation', f'grid_name_s:{grid_name}',
                        f'field_s:{field_name}', f'pre_transformation_file_path_s:"{source_file_path}"']
            docs = solr_utils.solr_query(query_fq)
            update_body = []
            transform = {}

            # If grid/field combination transformation exists, update transformation status
            # Otherwise initialize new transformation entry
            if len(docs) > 0:
                # Reset status fields
                transform['id'] = docs[0]['id']
                transform['transformation_in_progress_b'] = {"set": True}
                transform['success_b'] = {"set": False}
                update_body.append(transform)
                r = solr_utils.solr_update(update_body, r=True)
            else:
                # Initialize new transformation entry
                transform['type_s'] = 'transformation'
                transform['date_s'] = date
                transform['dataset_s'] = dataset_name
                transform['pre_transformation_file_path_s'] = source_file_path
                if hemi:
                    if hemi[0] == '_':
                        transform['hemisphere_s'] = hemi[1:]
                    else:
                        transform['hemisphere_s'] = hemi
                transform['origin_checksum_s'] = origin_checksum
                transform['grid_name_s'] = grid_name
                transform['field_s'] = field_name
                transform['transformation_in_progress_b'] = True
                transform['success_b'] = False
                update_body.append(transform)
                r = solr_utils.solr_update(update_body, r=True)

            if r.status_code != 200:
                verboseprint(
                    f'Failed to update Solr transformation status for {dataset_name} on {date}')

        # =====================================================
        # Run transformation
        # =====================================================
        verboseprint(f' - Running transformations for {file_name}')

        # Returns list of transformed DSs, one for each field in fields
        field_DSs = run_in_any_env(model_grid, grid_name, grid_type, fields,
                                   factors, ds, date, dataset_metadata, config,
                                   fill_values, verbose=verbose)

        # =====================================================
        # Save the output in netCDF format
        # =====================================================

        # Save each transformed granule for the current field
        for field, (field_DS, success) in zip(fields, field_DSs):
            field_name = field["name_s"]

            # time stuff
            data_time_scale = dataset_metadata['data_time_scale_s']
            if data_time_scale == 'daily':
                output_freq_code = 'AVG_DAY'
                rec_end = field_DS.time_bnds.values[0][1]
            elif data_time_scale == 'monthly':
                output_freq_code = 'AVG_MON'
                cur_year = int(date[:4])
                cur_month = int(date[5:7])

                if cur_month < 12:
                    cur_mon_year = np.datetime64(str(cur_year) + '-' +
                                                 str(cur_month+1).zfill(2) +
                                                 '-' + str(1).zfill(2), 'ns')
                    # for december we go up one year, and set month to january
                else:
                    cur_mon_year = np.datetime64(str(cur_year+1) + '-' +
                                                 str('01') +
                                                 '-' + str(1).zfill(2), 'ns')
                rec_end = cur_mon_year

            if 'DEBIAS_LOCEAN' in dataset_name:
                rec_end = field_DS.time.values[0] + np.timedelta64(1, 'D')

            tb, ct = ea.make_time_bounds_from_ds64(rec_end, output_freq_code)

            field_DS.time.values[0] = ct
            field_DS.time_bnds.values[0][0] = tb[0]
            field_DS.time_bnds.values[0][1] = tb[1]

            # field_DS.time_bnds.attrs['long_name'] = 'time bounds'

            field_DS = field_DS.drop('time_start')
            field_DS = field_DS.drop('time_end')

            # Change .bz2 file extension to .nc
            if 'bz2' in file_name:
                file_name = file_name[:-3] + 'nc'

            output_filename = f'{grid_name}_{field_name}_{file_name}'
            output_path = f'{output_dir}/{dataset_name}/transformed_products/{grid_name}/transformed/{field_name}/'
            transformed_location = f'{output_path}{output_filename}'

            Path(output_path).mkdir(parents=True, exist_ok=True)

            # save field_DS
            ea.save_to_disk(field_DS, output_filename[:-3], fill_values['binary'],
                            fill_values['netcdf'], Path(output_path),
                            Path(output_path), binary_dtype, grid_type, save_binary=False)

            # Query Solr for transformation entry
            query_fq = [f'dataset_s:{dataset_name}', 'type_s:transformation', f'grid_name_s:{grid_name}',
                        f'field_s:{field_name}', f'pre_transformation_file_path_s:"{source_file_path}"']

            docs = solr_utils.solr_query(query_fq)
            doc_id = solr_utils.solr_query(query_fq)[0]['id']

            transformation_successes = transformation_successes and success
            transformation_file_paths[f'{grid_name}_{field_name}_transformation_file_path_s'] = transformed_location

            # Update Solr transformation entry with file paths and status
            update_body = [
                {
                    "id": doc_id,
                    "filename_s": {"set": output_filename},
                    "transformation_file_path_s": {"set": transformed_location},
                    "transformation_completed_dt": {"set": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
                    "transformation_in_progress_b": {"set": False},
                    "success_b": {"set": success},
                    "transformation_checksum_s": {"set": file_utils.md5(transformed_location)},
                    "transformation_version_f": {"set": transformation_version}
                }
            ]

            r = solr_utils.solr_update(update_body, r=True)

            if r.status_code != 200:
                verboseprint(
                    f'Failed to update Solr transformation entry for {field["name_s"]} in {dataset_name} on {date}')

            if success and grid_name not in grids_updated:
                grids_updated.append(grid_name)

        # Always print regardless of verbosity
        print(
            f' - CPU id {os.getpid()} saving {file_name} output file for grid {grid_name}')

    # Query Solr for descendants entry by date
    query_fq = [f'dataset_s:{dataset_name}',
                'type_s:descendants', f'date_s:{date[:10]}*']
    if hemi:
        query_fq.append(f'hemisphere_s:{hemi[1:]}')

    docs = solr_utils.solr_query(query_fq)
    doc_id = solr_utils.solr_query(query_fq)[0]['id']

    # Update descendants entry in Solr
    update_body = [
        {
            "id": doc_id,
            "all_transformations_success_b": {"set": transformation_successes}
        }
    ]

    # Add transformaiton file path fields to descendants entry
    for key, path in transformation_file_paths.items():
        update_body[0][key] = {"set": path}

    r = solr_utils.solr_update(update_body, r=True)

    if r.status_code != 200:
        verboseprint(
            f'Failed to update Solr with descendants information for {dataset_name} on {date}')

    return grids_updated, date[:4]


def run_in_any_env(model_grid, grid_name, grid_type, fields, factors, ds, record_date, dataset_metadata, config, fill_values, verbose=True):
    """
    Function that actually performs the transformations. Returns a list of transformed
    xarray datasets, one dataset for each field being transformed for the given grid.
    """
    verboseprint = print if verbose else lambda *a, **k: None

    # Check if ends in z and drop it if it does
    if record_date[-1] == 'Z':
        record_date = record_date[:-1]

    array_precision = getattr(np, config['array_precision'])
    record_file_name = ds.attrs['original_file_name']
    extra_information = config['extra_information']
    time_zone_included_with_time = config['time_zone_included_with_time']
    data_time_scale = dataset_metadata['data_time_scale_s'].upper()

    field_DSs = []

    original_dataset_metadata = {
        key: dataset_metadata[key] for key in dataset_metadata.keys() if 'original' in key}

    pre_transformations = config['pre_transformation_steps']
    post_transformations = config['post_transformation_steps']

    # =====================================================
    # Pre transformation functions
    # =====================================================
    if pre_transformations:
        for func_to_run in pre_transformations:
            callable_func = getattr(ea, func_to_run)
            try:
                ds = callable_func(ds)
            except Exception as e:
                log.exception(f'Pre-transformation {func_to_run} failed: {e}')
                return []

    # =====================================================
    # Loop through fields to transform
    # =====================================================
    for data_field_info in fields:
        field_name = data_field_info['name_s']
        standard_name = data_field_info['standard_name_s']
        long_name = data_field_info['long_name_s']
        units = data_field_info['units_s']

        verboseprint(
            f'    - Transforming {record_file_name} for field {field_name}')

        try:
            field_DA = ea.generalized_transform_to_model_grid_solr(data_field_info, record_date, model_grid, grid_type,
                                                                   array_precision, record_file_name, data_time_scale,
                                                                   extra_information, ds, factors, time_zone_included_with_time,
                                                                   grid_name)
            success = True

            # =====================================================
            # Post transformation functions
            # =====================================================
            if post_transformations:
                for func_to_run in post_transformations:
                    callable_func = getattr(ea, func_to_run)

                    try:
                        field_DA = callable_func(field_DA, field_name)
                    except Exception as e:
                        log.exception(
                            f'Post-transformation {func_to_run} failed: {e}')
                        field_DA = ea.make_empty_record(standard_name, long_name, units,
                                                        record_date, model_grid,
                                                        grid_type, array_precision)
                        success = False
                        break

            field_DA.attrs['valid_min'] = np.nanmin(field_DA.values)
            field_DA.attrs['valid_max'] = np.nanmax(field_DA.values)

        except Exception as e:
            log.exception(f'Transformation failed: {e}')
            field_DA = ea.make_empty_record(standard_name, long_name, units,
                                            record_date, model_grid,
                                            grid_type, array_precision)
            success = False

        field_DA.values = \
            np.where(np.isnan(field_DA.values),
                     fill_values['netcdf'], field_DA.values)

        # Make dataarray into dataset
        field_DS = field_DA.to_dataset()

        ds_meta = {}

        # Dataset metadata
        if 'title' in model_grid:
            ds_meta['interpolated_grid'] = model_grid.title
        else:
            ds_meta['interpolated_grid'] = model_grid.name
        ds_meta['model_grid_type'] = grid_type
        ds_meta['original_dataset_title'] = original_dataset_metadata['original_dataset_title_s']
        ds_meta['original_dataset_short_name'] = original_dataset_metadata['original_dataset_short_name_s']
        ds_meta['original_dataset_url'] = original_dataset_metadata['original_dataset_url_s']
        ds_meta['original_dataset_reference'] = original_dataset_metadata['original_dataset_reference_s']
        ds_meta['original_dataset_doi'] = original_dataset_metadata['original_dataset_doi_s']
        ds_meta['interpolated_grid_id'] = grid_name
        ds_meta['transformation_version'] = config['t_version']
        ds_meta['notes'] = config['notes']
        field_DS = field_DS.assign_attrs(ds_meta)

        # add time_bnds coordinate
        # [start_time, end_time] dimensions
        start_time = field_DS.time_start.values
        end_time = field_DS.time_end.values

        time_bnds = np.array(
            [start_time, end_time], dtype='datetime64')
        time_bnds = time_bnds.T
        field_DS = field_DS.assign_coords(
            {'time_bnds': (['time', 'nv'], time_bnds)})

        field_DS.time.attrs.update(bounds='time_bnds')

        field_DSs.append((field_DS, success))

    return field_DSs
