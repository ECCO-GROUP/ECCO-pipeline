import json
import logging
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import xarray as xr
from netCDF4 import default_fillvals  # pylint: disable=no-name-in-module
from utils import solr_utils

logging.config.fileConfig('logs/log.ini', disable_existing_loggers=False)
log = logging.getLogger(__name__)
np.warnings.filterwarnings('ignore')

try:
    sys.path.append(str(Path('../ecco-cloud-utils/').resolve()))
    import ecco_cloud_utils as ea  # pylint: disable=import-error
except Exception as e:
    log.exception(e)


def years_to_aggregate(dataset_name, grid_name):

    years = []

    fq = [f'dataset_s:{dataset_name}',
          'type_s:transformation', f'grid_name_s:{grid_name}']
    r = solr_utils.solr_query(fq)
    transformation_years = list(set([t['date_s'][:4] for t in r]))
    transformation_years.sort()
    transformation_docs = r

    # Years with transformations that exist for this dataset and this grid
    for year in transformation_years:
        # We want to see if we need to aggregate this year again
        # 1. check if aggregation exists - if not add it to years to aggregate
        # 2. if it does - compare prcessing times:
        #   - if aggregation time is later than all transform times for that year
        #     no need to aggregate
        #   - if at least one transformation occured after agg time, year needs to
        #     be aggregated
        fq = [f'dataset_s:{dataset_name}', 'type_s:aggregation',
              f'grid_name_s:{grid_name}', f'year_s:{year}']
        r = solr_utils.solr_query(fq)

        if r:
            agg_time = r[0]['aggregation_time_dt']
            for t in transformation_docs:
                if t['date_s'][:4] != year:
                    continue
                if t['transformation_completed_dt'] > agg_time:
                    years.append(year)
                    break
        else:
            years.append(year)

    return years


def aggregation(output_dir, config, grids_to_use=[]):
    """
    Aggregates data into annual files, saves them, and updates Solr
    """

    # =====================================================
    # Code to import ecco utils locally...
    # =====================================================
    sys.path.append('../ecco-cloud-utils/')
    import ecco_cloud_utils as ea  # pylint: disable=import-error

    # =====================================================
    # Set configuration options and Solr metadata
    # =====================================================
    dataset_name = config['ds_name']

    # =====================================================
    # Pull metadata from Solr
    # =====================================================
    fq = ['type_s:grid']
    grids = [grid for grid in solr_utils.solr_query(fq)]

    # Update grids to only use those in grids_to_use
    if grids_to_use:
        grids = [grid for grid in grids if grid['grid_name_s'] in grids_to_use]

    # Query Solr for fields
    fq = ['type_s:field', f'dataset_s:{dataset_name}']
    fields = solr_utils.solr_query(fq)

    # Query Solr for dataset metadata
    fq = ['type_s:dataset', f'dataset_s:{dataset_name}']
    dataset_metadata = solr_utils.solr_query(fq)[0]

    aggregate_all_years = False
    aggregation_version = str(config['a_version'])
    if 'aggregation_version_s' in dataset_metadata.keys():
        existing_aggregation_version = dataset_metadata['aggregation_version_s']
        if existing_aggregation_version != aggregation_version:
            aggregate_all_years = True

    data_time_scale = dataset_metadata['data_time_scale_s']

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

    update_body = []

    aggregation_successes = True

    # =====================================================
    # Loop through grids
    # =====================================================
    for grid in grids:

        grid_path = grid['grid_path_s']
        grid_name = grid['grid_name_s']
        grid_type = grid['grid_type_s']

        # Only aggregate years with updated transformations
        # Based on years_updated_ss field in dataset Solr entry
        if aggregate_all_years:
            start_year = int(dataset_metadata['start_date_dt'][:4])
            end_year = int(dataset_metadata['end_date_dt'][:4])
            years = [str(year) for year in range(start_year, end_year + 1)]
        else:
            years = years_to_aggregate(dataset_name, grid_name)

        if not years:
            # If no years to aggregate for this grid, continue to next grid
            print(f'No updated years to aggregate for {grid_name}')
            continue

        print(
            f'\nAggregating years {min(years)} to {max(years)} for {grid_name}\n')

        model_grid = xr.open_dataset(grid_path, decode_times=True)

        # =====================================================
        # Loop through years
        # =====================================================
        for year in years:

            # Construct list of dates corresponding to data time scale
            if data_time_scale == 'daily':
                dates_in_year = np.arange(
                    f'{year}-01-01', f'{int(year)+1}-01-01', dtype='datetime64[D]')
            elif data_time_scale == 'monthly':
                dates_in_year = np.arange(
                    f'{year}-01', f'{int(year)+1}-01', dtype='datetime64[M]')
                dates_in_year = [f'{date}-01' for date in dates_in_year]

            # =====================================================
            # Loop through fields
            # =====================================================
            for field in fields:

                field_name = field['name_s']

                print(
                    f'\n====== Aggregating {str(year)}_{grid_name}_{field_name} ======\n')

                json_output = {}
                transformations = []
                json_output['dataset'] = dataset_metadata

                daily_DS_year = []

                # =====================================================
                # Loop through dates
                # =====================================================
                for date in dates_in_year:
                    # variable to store name of data values in dataset
                    data_var = f'{field_name}_interpolated_to_{grid_name}'

                    # Query for date
                    fq = [f'dataset_s:{dataset_name}', 'type_s:transformation',
                          f'grid_name_s:{grid_name}', f'field_s:{field_name}', f'date_s:{date}*']

                    docs = solr_utils.solr_query(fq)

                    # If first of month is not found, query with 7 day tolerance only for monthly data
                    if not docs and data_time_scale == 'monthly':
                        if config['monthly_tolerance']:
                            tolerance = int(config['monthly_tolerance'])
                        else:
                            tolerance = 8
                        start_month_date = datetime.strptime(date, '%Y-%m-%d')
                        tolerance_days = []

                        for i in range(1, tolerance):
                            plus_date = start_month_date + timedelta(days=i)
                            neg_date = start_month_date - timedelta(days=i)

                            tolerance_days.append(
                                datetime.strftime(plus_date, '%Y-%m-%d'))
                            tolerance_days.append(
                                datetime.strftime(neg_date, '%Y-%m-%d'))

                        for tol_date in tolerance_days:
                            fq = [f'dataset_s:{dataset_name}', 'type_s:transformation',
                                  f'grid_name_s:{grid_name}', f'field_s:{field_name}', f'date_s:{tol_date}*']
                            docs = solr_utils.solr_query(fq)

                            if docs:
                                break

                    # If transformed file is present for date, grid, and field combination
                    # open the file, otherwise make empty record
                    opened_datasets = []
                    for doc in docs:
                        data_DS = xr.open_dataset(
                            doc['transformation_file_path_s'], decode_times=True)

                        # get name of data variable in the dataset
                        # to be used when accessing the values of the transformed data
                        # since the transformed files only have one variable, we index at zero to get it
                        # type is str
                        data_var = list(data_DS.keys())[0]

                        opened_datasets.append((data_DS, data_var))

                        # Update JSON transformations list
                        fq = [f'dataset_s:{dataset_name}', 'type_s:granule',
                              f'pre_transformation_file_path_s:"{doc["pre_transformation_file_path_s"]}"']
                        harvested_metadata = solr_utils.solr_query(fq)

                        transformation_metadata = doc
                        transformation_metadata['harvested'] = harvested_metadata
                        transformations.append(transformation_metadata)

                    # If there are more than one files for this grid/field/date combination (implies hemisphered data),
                    # combine hemispheres on nonempty datafile, if present.
                    if len(opened_datasets) == 2:
                        first_DS = opened_datasets[0][0]
                        first_DS_name = opened_datasets[0][1]
                        second_DS = opened_datasets[1][0]
                        second_DS_name = opened_datasets[1][1]
                        if ~np.isnan(first_DS[first_DS_name].values).all():
                            data_DS = first_DS.copy()
                            data_DS[first_DS_name].values = np.where(
                                np.isnan(data_DS[first_DS_name].values), second_DS[second_DS_name].values, data_DS[first_DS_name].values)
                            data_var = first_DS_name
                        else:
                            data_DS = second_DS.copy()
                            data_DS[second_DS_name].values = np.where(
                                np.isnan(data_DS[second_DS_name].values), first_DS[first_DS_name].values, data_DS[second_DS_name].values)
                            data_var = second_DS_name
                    elif len(opened_datasets) == 1:
                        data_DS = opened_datasets[0][0]
                        data_var = opened_datasets[0][1]
                    else:
                        data_DA = ea.make_empty_record(
                            field['standard_name_s'], field['long_name_s'], field['units_s'], date, model_grid, grid_type, array_precision)
                        data_DA.name = data_var

                        empty_record_attrs = data_DA.attrs
                        empty_record_attrs['original_field_name'] = field_name
                        empty_record_attrs['interpolation_date'] = str(
                            np.datetime64(datetime.now(), 'D'))
                        data_DA.attrs = empty_record_attrs

                        data_DS = data_DA.to_dataset()

                        # add time_bnds coordinate
                        # [start_time, end_time] dimensions
                        # MONTHLY cannot use timedelta64 since it has a variable
                        # number of ns/s/d. DAILY can so we use it.
                        if data_time_scale.upper() == 'MONTHLY':
                            end_time = str(data_DS.time_end.values[0])
                            month = str(np.datetime64(end_time, 'M') + 1)
                            end_time = [str(np.datetime64(month, 'ns'))]
                        elif data_time_scale.upper() == 'DAILY':
                            end_time = data_DS.time_end.values + \
                                np.timedelta64(1, 'D')

                        _, ct = ea.make_time_bounds_from_ds64(
                            np.datetime64(end_time[0], 'ns'), 'AVG_MON')
                        data_DS.time.values[0] = ct

                        start_time = data_DS.time_start.values

                        time_bnds = np.array(
                            [start_time, end_time], dtype='datetime64')
                        time_bnds = time_bnds.T

                        data_DS = data_DS.assign_coords(
                            {'time_bnds': (['time', 'nv'], time_bnds)})

                        data_DS.time.attrs.update(bounds='time_bnds')

                        data_DS = data_DS.drop('time_start')
                        data_DS = data_DS.drop('time_end')

                    # Append each day's data to annual list
                    daily_DS_year.append(data_DS)

                # Concatenate all data files within annual list
                daily_DS_year_merged = xr.concat(
                    (daily_DS_year), dim='time', combine_attrs='no_conflicts')
                data_var = list(daily_DS_year_merged.keys())[0]

                daily_DS_year_merged.attrs['aggregation_version'] = config['a_version']

                daily_DS_year_merged[data_var].attrs['valid_min'] = np.nanmin(
                    daily_DS_year_merged[data_var].values)
                daily_DS_year_merged[data_var].attrs['valid_max'] = np.nanmax(
                    daily_DS_year_merged[data_var].values)

                remove_keys = []
                for (key, _) in daily_DS_year_merged[data_var].attrs.items():
                    if ('original' in key and key != 'original_field_name'):
                        remove_keys.append(key)

                for key in remove_keys:
                    del daily_DS_year_merged[data_var].attrs[key]

                # Create filenames based on date time scale
                # If data time scale is monthly, shortest_filename is monthly
                shortest_filename = f'{dataset_name}_{grid_name}_{data_time_scale.upper()}_{field_name}_{year}'
                monthly_filename = f'{dataset_name}_{grid_name}_MONTHLY_{field_name}_{year}'

                output_filenames = {'shortest': shortest_filename,
                                    'monthly': monthly_filename}

                output_path = f'{output_dir}/{dataset_name}/transformed_products/{grid_name}/aggregated/{field_name}/'

                bin_output_dir = Path(output_path) / 'bin'
                bin_output_dir.mkdir(parents=True, exist_ok=True)

                netCDF_output_dir = Path(output_path) / 'netCDF'
                netCDF_output_dir.mkdir(parents=True, exist_ok=True)

                # generalized_aggregate_and_save expects Paths
                output_dirs = {'binary': bin_output_dir,
                               'netcdf': netCDF_output_dir}

                # used for Solr docs metadata
                solr_output_filepaths = {'daily_bin': f'{output_path}bin/{shortest_filename}',
                                         'daily_netCDF': f'{output_path}netCDF/{shortest_filename}.nc',
                                         'monthly_bin': f'{output_path}bin/{monthly_filename}',
                                         'monthly_netCDF': f'{output_path}netCDF/{monthly_filename}.nc'}

                uuids = [str(uuid.uuid1()), str(uuid.uuid1())]

                try:
                    # Performs the aggreagtion of the yearly data, and saves it
                    empty_year = ea.generalized_aggregate_and_save(daily_DS_year_merged,
                                                                   data_var,
                                                                   config['do_monthly_aggregation'],
                                                                   int(year),
                                                                   config['skipna_in_mean'],
                                                                   output_filenames,
                                                                   fill_values,
                                                                   output_dirs,
                                                                   binary_dtype,
                                                                   grid_type,
                                                                   on_aws=False,
                                                                   save_binary=config['save_binary'],
                                                                   save_netcdf=config['save_netcdf'],
                                                                   remove_nan_days_from_data=config[
                                                                       'remove_nan_days_from_data'],
                                                                   data_time_scale=data_time_scale,
                                                                   uuids=uuids)

                    print(
                        f' - Saving {str(year)}_{grid_name}_{field_name} file(s) DONE')

                    success = True

                except Exception as e:
                    log.exception(f'{dataset_name} aggregation error! {e}')
                    empty_year = True
                    success = False
                    solr_output_filepaths = {'daily_bin': '',
                                             'daily_netCDF': '',
                                             'monthly_bin': '',
                                             'monthly_netCDF': ''}

                aggregation_successes = aggregation_successes and success
                empty_year = empty_year and success

                if empty_year:
                    solr_output_filepaths = {'daily_bin': '',
                                             'daily_netCDF': '',
                                             'monthly_bin': '',
                                             'monthly_netCDF': ''}

                # Query Solr for existing aggregation
                fq = [f'dataset_s:{dataset_name}', 'type_s:aggregation',
                      f'grid_name_s:{grid_name}', f'field_s:{field_name}', f'year_s:{year}']
                docs = solr_utils.solr_query(fq)

                # If aggregation exists, update using Solr entry id
                if len(docs) > 0:
                    doc_id = docs[0]['id']
                    update_body = [
                        {
                            "id": doc_id,
                            "aggregation_time_dt": {"set": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
                            "aggregation_version_s": {"set": aggregation_version}
                        }
                    ]

                    # Update file paths according to the data time scale and do monthly aggregation config field
                    if (data_time_scale == 'daily') and (config['do_monthly_aggregation']):
                        update_body[0]["aggregated_daily_bin_path_s"] = {
                            "set": solr_output_filepaths['daily_bin']}
                        update_body[0]["aggregated_daily_netCDF_path_s"] = {
                            "set": solr_output_filepaths['daily_netCDF']}
                        update_body[0]["aggregated_monthly_bin_path_s"] = {
                            "set": solr_output_filepaths['monthly_bin']}
                        update_body[0]["aggregated_monthly_netCDF_path_s"] = {
                            "set": solr_output_filepaths['monthly_netCDF']}
                        update_body[0]["daily_aggregated_uuid_s"] = {
                            "set": uuids[0]}
                        update_body[0]["monthly_aggregated_uuid_s"] = {
                            "set": uuids[1]}
                    elif (data_time_scale == 'daily') and not (config['do_monthly_aggregation']):
                        update_body[0]["aggregated_daily_bin_path_s"] = {
                            "set": solr_output_filepaths['daily_bin']}
                        update_body[0]["aggregated_daily_netCDF_path_s"] = {
                            "set": solr_output_filepaths['daily_netCDF']}
                        update_body[0]["daily_aggregated_uuid_s"] = {
                            "set": uuids[0]}
                    elif data_time_scale == 'monthly':
                        update_body[0]["aggregated_monthly_bin_path_s"] = {
                            "set": solr_output_filepaths['monthly_bin']}
                        update_body[0]["aggregated_monthly_netCDF_path_s"] = {
                            "set": solr_output_filepaths['monthly_netCDF']}
                        update_body[0]["monthly_aggregated_uuid_s"] = {
                            "set": uuids[1]}

                    if empty_year:
                        update_body[0]["notes_s"] = {
                            "set": 'Empty year (no data present in grid), not saving to disk.'}
                    else:
                        update_body[0]["notes_s"] = {"set": ''}

                else:
                    # Create new aggregation entry if it doesn't exist
                    update_body = [
                        {
                            "type_s": 'aggregation',
                            "dataset_s": dataset_name,
                            "year_s": year,
                            "grid_name_s": grid_name,
                            "field_s": field_name,
                            "aggregation_time_dt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "aggregation_success_b": success,
                            "aggregation_version_s": aggregation_version
                        }
                    ]

                    # Update file paths according to the data time scale and do monthly aggregation config field
                    if (data_time_scale == 'daily') and (config['do_monthly_aggregation']):
                        update_body[0]["aggregated_daily_bin_path_s"] = {
                            "set": solr_output_filepaths['daily_bin']}
                        update_body[0]["aggregated_daily_netCDF_path_s"] = {
                            "set": solr_output_filepaths['daily_netCDF']}
                        update_body[0]["aggregated_monthly_bin_path_s"] = {
                            "set": solr_output_filepaths['monthly_bin']}
                        update_body[0]["aggregated_monthly_netCDF_path_s"] = {
                            "set": solr_output_filepaths['monthly_netCDF']}
                        update_body[0]["daily_aggregated_uuid_s"] = {
                            "set": uuids[0]}
                        update_body[0]["monthly_aggregated_uuid_s"] = {
                            "set": uuids[1]}
                    elif (data_time_scale == 'daily') and (not config['do_monthly_aggregation']):
                        update_body[0]["aggregated_daily_bin_path_s"] = {
                            "set": solr_output_filepaths['daily_bin']}
                        update_body[0]["aggregated_daily_netCDF_path_s"] = {
                            "set": solr_output_filepaths['daily_netCDF']}
                        update_body[0]["daily_aggregated_uuid_s"] = {
                            "set": uuids[0]}
                    elif data_time_scale == 'monthly':
                        update_body[0]["aggregated_monthly_bin_path_s"] = {
                            "set": solr_output_filepaths['monthly_bin']}
                        update_body[0]["aggregated_monthly_netCDF_path_s"] = {
                            "set": solr_output_filepaths['monthly_netCDF']}
                        update_body[0]["monthly_aggregated_uuid_s"] = {
                            "set": uuids[1]}

                    if empty_year:
                        update_body[0]["notes_s"] = {
                            "set": 'Empty year (no data present in grid), not saving to disk.'}
                    else:
                        update_body[0]["notes_s"] = {"set": ''}

                r = solr_utils.solr_update(update_body, r=True)

                if r.status_code != 200:
                    print(
                        f'Failed to update Solr aggregation entry for {field_name} in {dataset_name} for {year} and grid {grid_name}')

                # Query for descendants entries from this year
                fq = ['type_s:descendants',
                      f'dataset_s:{dataset_name}', f'date_s:{year}*']
                existing_descendants_docs = solr_utils.solr_query(fq)

                # if descendants entries already exist, update them
                if len(existing_descendants_docs) > 0:
                    for doc in existing_descendants_docs:
                        doc_id = doc['id']

                        update_body = [
                            {
                                "id": doc_id,
                                "all_aggregation_success_b": {"set": aggregation_successes}
                            }
                        ]

                        # Add aggregation file path fields to descendants entry
                        for key, value in solr_output_filepaths.items():
                            update_body[0][f'{grid_name}_{field_name}_aggregated_{key}_path_s'] = {
                                "set": value}

                        r = solr_utils.solr_update(update_body, r=True)

                        if r.status_code != 200:
                            print(
                                f'Failed to update Solr aggregation entry for {field_name} in {dataset_name} for {year} and grid {grid_name}')

                fq = [f'dataset_s:{dataset_name}', 'type_s:aggregation',
                      f'grid_name_s:{grid_name}', f'field_s:{field_name}', f'year_s:{year}']
                docs = solr_utils.solr_query(fq)

                # Export annual descendants JSON file for each aggregation created
                print(
                    f' - Exporting {year} descendants for grid {grid_name} and field {field_name}')
                json_output['aggregation'] = docs
                json_output['transformations'] = transformations
                json_output_path = f'{output_dir}/{dataset_name}/transformed_products/{grid_name}/aggregated/{field_name}/{dataset_name}_{field_name}_{grid_name}_{year}_descendants'
                with open(json_output_path, 'w') as f:
                    resp_out = json.dumps(json_output, indent=4)
                    f.write(resp_out)

    # Query Solr for successful aggregation documents
    fq = [f'dataset_s:{dataset_name}',
          'type_s:aggregation', 'aggregation_success_b:true']
    successful_aggregations = solr_utils.solr_query(fq)

    # Query Solr for failed aggregation documents
    fq = [f'dataset_s:{dataset_name}',
          'type_s:aggregation', 'aggregation_success_b:false']
    failed_aggregations = solr_utils.solr_query(fq)

    aggregation_status = 'All aggregations successful'

    if not successful_aggregations and not failed_aggregations:
        aggregation_status = 'No aggregations performed'
    elif not successful_aggregations:
        aggregation_status = 'No successful aggregations'
    elif failed_aggregations:
        aggregation_status = f'{len(failed_aggregations)} aggregations failed'

    # Update Solr dataset entry status and years_updated to empty
    update_body = [
        {
            "id": dataset_metadata['id'],
            "aggregation_version_s": {"set": aggregation_version},
            "aggregation_status_s": {"set": aggregation_status}
        }
    ]

    r = solr_utils.solr_update(update_body, r=True)

    if r.status_code == 200:
        print(
            f'\nSuccessfully updated Solr with aggregation information for {dataset_name}\n')
    else:
        print(
            f'\nFailed to update Solr dataset entry with aggregation information for {dataset_name}\n')

    return aggregation_status
