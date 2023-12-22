import itertools
import logging
from logging.handlers import QueueHandler
from collections import defaultdict
from multiprocessing import Queue, current_process, Pool, Manager
import os
from typing import Iterable
import xarray as xr

from dataset import Dataset
from utils import solr_utils, log_config
from transformations.grid_transformation import transform
from transformations.transformation import Transformation
from transformations.preload import Factors, Grids
from conf.global_settings import OUTPUT_DIR

loaded_factors = Factors()
loaded_grids = Grids()

def logging_process(queue: Queue, log_filename: str):
    '''
    Process to run during multiprocessing. Allows for logging of each process.
    '''
    log_config.configure_logging(True, log_filename=log_filename)

    # report that the logging process is running
    logging.debug(f'Logger process running.')
    # run forever
    while True:
        # consume a log message, block until one arrives
        message = queue.get()
        # check for shutdown
        if message is None:
            logging.debug(f'Logger process shutting down.')
            break
        # log the message
        logging.getLogger().handle(message)

def get_remaining_transformations(dataset: Dataset, granule_file_path: str, grids: Iterable[str]) -> dict:
    """
    Given a single granule, the function uses Solr to find all combinations of
    grids and fields that have yet to be transformed. It returns a dictionary
    where the keys are grids and the values are lists of fields.

    if a transformation entry exists for this granule, check to see if the
    checksum of the harvested granule matches the checksum recorded in the
    transformation entry for this granule, if not then we have to retransform
    also check to see if the version of the transformation code recorded in
    the entry matches the current version of the transformation code, if not
    redo the transformation.

    these checks are made for each grid/field pair associated with the harvested granule
    """

    # Cartesian product of grid/field combinations
    grid_field_combinations = list(itertools.product(grids, dataset.fields))

    # Build dictionary of remaining transformations
    # -- grid_field_dict has grid key, entries is list of fields
    grid_field_dict = defaultdict(list)

    # Query for existing transformations
    fq = [f'dataset_s:{dataset.ds_name}', 'type_s:transformation',
          f'pre_transformation_file_path_s:"{granule_file_path}"']
    docs = solr_utils.solr_query(fq)

    # No transformations exist yet, so all must be done
    if not docs:
        for grid, field in grid_field_combinations:
            grid_field_dict[grid].append(field)
        logging.info(f'{sum([len(v) for v in grid_field_dict.values()])} remaining transformations for {granule_file_path.split("/")[-1]}')
        return dict(grid_field_dict)

    # Dictionary where key is grid, field tuple and value is harvested granule checksum
    # For existing transformations pulled from Solr
    existing_transformations = {(doc['grid_name_s'], doc['field_s']): doc['origin_checksum_s'] for doc in docs}

    drop_list = []

    # Loop through all grid/field combos, checking if processing is required
    for (grid, field) in grid_field_combinations:
        # If transformation exists, must compare checksums and versions for updates
        if (grid, field.name) in existing_transformations:

            # Query for harvested granule checksum
            fq = [f'dataset_s:{dataset.ds_name}', 'type_s:granule',
                  f'pre_transformation_file_path_s:"{granule_file_path}"']
            harvested_checksum = solr_utils.solr_query(fq)[0]['checksum_s']

            origin_checksum = existing_transformations[(grid, field.name)]

            # Query for existing transformation
            fq = [f'dataset_s:{dataset.ds_name}', 'type_s:transformation',
                  f'pre_transformation_file_path_s:"{granule_file_path}"',
                  f'field_s:{field.name}']
            transformation = solr_utils.solr_query(fq)[0]

            # Triple if:
            # 1. do we have a version entry,
            # 2. compare transformation version number and current transformation version number
            # 3. compare checksum of harvested file (currently in solr) and checksum
            #    of the harvested file that was previously transformed (recorded in transformation entry)
            if ('success_b' in transformation.keys() and transformation['success_b'] == True) and \
                ('transformation_version_f' in transformation.keys() and transformation['transformation_version_f'] == dataset.t_version) and \
                    origin_checksum == harvested_checksum:
                logging.debug(f'No need to transform {granule_file_path} for grid {grid} and field {field.name}')
                # all tests passed, we do not need to redo the transformation
                # for this grid/field pair

                # Add grid/field combination to drop_list
                drop_list.append((grid, field))

    # Remove drop_list grid/field combinations from list of remaining transformations
    grid_field_combinations = [combo for combo in grid_field_combinations if combo not in drop_list]

    for grid, field in grid_field_combinations:
        grid_field_dict[grid].append(field)
    logging.info(f'{sum([len(v) for v in grid_field_dict.values()])} remaining transformations for {granule_file_path.split("/")[-1]}')
    return dict(grid_field_dict)


def multiprocess_transformation(config: dict, granule: dict, dataset: Dataset, grids: Iterable[str], log_filename: str, loaded_factors: Factors, loaded_grids: Grids):
    """
    Callable function that performs the actual transformation on a granule.
    """
    log_config.configure_logging(True, log_filename=log_filename)
       
    process = current_process()
    granule_filepath = granule.get('pre_transformation_file_path_s')
    granule_date = granule.get('date_s')

    # Skips granules that weren't harvested properly
    if not granule_filepath or granule.get('file_size_l') < 100:
        logging.exception(f'Granule {granule_filepath} was not harvested properly. Skipping.')
        return ('', '')

    # Get transformations to be completed for this file
    try:
        remaining_transformations = get_remaining_transformations(dataset, granule_filepath, grids)
    except Exception as e:
        logging.exception(f'Unexpected error getting remaining transformations. {e}')
        raise RuntimeError(e)
    
    # Perform remaining transformations
    if remaining_transformations:
        try:
            transform(granule_filepath, remaining_transformations, config, granule_date, loaded_factors, loaded_grids)
        except Exception as e:
            logging.exception(f'Error transforming {granule_filepath}: {e}')
    else:
        logging.debug(f'No new transformations for {granule["filename_s"]}')


def find_data_for_factors(config: dict, harvested_granules: Iterable[dict]) -> Iterable[dict]:
    '''
    Returns Solr granule entry (two in the case of hemispherical data) to be used
    to generate factors
    '''
    data_for_factors = []
    nh_added = False
    sh_added = False
    # Find appropriate granule(s) to use for factor calculation
    for granule in harvested_granules:
        if 'hemisphere_s' in granule.keys():
            hemi = f'_{granule["hemisphere_s"]}_'
        else:
            hemi = ''
        if granule.get('pre_transformation_file_path_s'):
            if hemi:
                # Get one of each
                if hemi == config['hemi_pattern']['north'] and not nh_added:
                    data_for_factors.append(granule)
                    nh_added = True
                elif hemi == config['hemi_pattern']['south'] and not sh_added:
                    data_for_factors.append(granule)
                    sh_added = True
                if nh_added and sh_added:
                    return data_for_factors
            else:
                data_for_factors.append(granule)
                return data_for_factors


def pregenerate_factors(config: dict, grids: Iterable[str], harvested_granules: Iterable[dict]):
    '''
    Generates factors for all grids used for the given transformation version. Loads them into
    Factors object which is used to reduce I/O.
    '''
    for grid in grids:
        loaded_grids.set_grid(f'grids/{grid}.nc')
        for granule in find_data_for_factors(config, harvested_granules):
            grid_ds = xr.open_dataset(f'grids/{grid}.nc')
            T = Transformation(config, granule['pre_transformation_file_path_s'], '1972-01-01')
            T.make_factors(grid_ds)
            
            factors_file = f'{grid_ds.name}{T.hemi}_v{T.transformation_version}_factors'
            factors_path = os.path.join(OUTPUT_DIR, T.ds_name, 'transformed_products', grid_ds.name, factors_file)
            
            loaded_factors.set_factors(factors_path)


def main(config: dict, user_cpus: int = 1, grids_to_use: Iterable[str]=[], log_filename: str='') -> str:
    """
    This function performs all remaining grid/field transformations for all harvested
    granules for a dataset. It also makes use of multiprocessing to perform multiple
    transformations at the same time. After all transformations have been attempted,
    the Solr dataset entry is updated with additional metadata.
    """
    dataset = Dataset(config)

    # Get all harvested granules for this dataset
    fq = [f'dataset_s:{dataset.ds_name}', 'type_s:granule', 'harvest_success_b:true']
    harvested_granules = solr_utils.solr_query(fq)

    if not harvested_granules:
        logging.info(f'No harvested granules found in solr for {dataset.ds_name}')
        return f'No transformations performed'

    # Query for grids
    if not grids_to_use:
        fq = ['type_s:grid']
        docs = solr_utils.solr_query(fq)
        grids = [doc['grid_name_s'] for doc in docs]
    else:
        grids = grids_to_use
        
    pregenerate_factors(config, grids, harvested_granules)

    job_params = [(config, granule, dataset, grids, log_filename, loaded_factors, loaded_grids) for granule in harvested_granules]

    # BEGIN MULTIPROCESSING
    if user_cpus == 1:
        logging.info('Not using multiprocessing to do transformation')
        for job_param in job_params:
            multiprocess_transformation(*job_param)
    else:
        logging.info(f'Using {user_cpus} CPUs to do multiprocess transformation')
        with Manager() as manager:
            queue = manager.Queue()
            # add a handler that uses the shared queue
            queue_handler = QueueHandler(queue)
            logging.getLogger().addHandler(queue_handler)
            
            with Pool(processes=user_cpus) as pool:               
                # issue a long running task to receive logging messages
                _ = pool.apply_async(logging_process, args=(queue,log_filename,))

                pool.starmap(multiprocess_transformation, job_params)
                
                queue.put(None)
                pool.close()
                pool.join()
                
            logging.getLogger().removeHandler(queue_handler)

    # Query Solr for dataset metadata
    fq = [f'dataset_s:{dataset.ds_name}', 'type_s:dataset']
    dataset_metadata = solr_utils.solr_query(fq)[0]

    # Query Solr for successful transformation documents
    fq = [f'dataset_s:{dataset.ds_name}', 'type_s:transformation', 'success_b:true']
    successful_transformations = solr_utils.solr_query(fq)

    # Query Solr for failed transformation documents
    fq = [f'dataset_s:{dataset.ds_name}', 'type_s:transformation', 'success_b:false']
    failed_transformations = solr_utils.solr_query(fq)

    transformation_status = f'All transformations successful'

    if not successful_transformations and not failed_transformations:
        transformation_status = f'No transformations performed'
    elif not successful_transformations:
        transformation_status = f'No successful transformations'
    elif failed_transformations:
        transformation_status = f'{len(failed_transformations)} transformations failed'

    # Update Solr dataset entry status to transformed
    update_body = [{
        "id": dataset_metadata['id'],
        "transformation_status_s": {"set": transformation_status},
    }]

    r = solr_utils.solr_update(update_body, r=True)

    if r.status_code == 200:
        logging.debug(f'Successfully updated Solr with transformation information for {dataset.ds_name}')
    else:
        logging.exception(f'Failed to update Solr with transformation information for {dataset.ds_name}')

    return transformation_status