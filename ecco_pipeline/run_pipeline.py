import argparse
import logging
import logging.config
import os
from collections import defaultdict
from multiprocessing import cpu_count
from pathlib import Path

from conf.global_settings import OUTPUT_DIR, SOLR_COLLECTION
import grids_to_solr
from grid_transformation import grid_transformation_local
from aggregation import aggregation
from utils import solr_utils

import requests
import yaml

###########
# Perform set up and verify system elements
###########

logging.config.fileConfig('logs/log.ini', disable_existing_loggers=False)
log = logging.getLogger(__name__)

# Set package logging level to WARNING
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Verify output directory is valid
if not Path.is_dir(OUTPUT_DIR):
    print('Missing output directory. Please fill in. Exiting.')
    log.fatal('Missing output directory. Please fill in. Exiting.')
    exit()
print(f'\nUsing output directory: {OUTPUT_DIR}')
print(f'\nUsing Solr collection: {SOLR_COLLECTION}')

# Verify solr is running
try:
    solr_utils.ping_solr()
except requests.ConnectionError:
    print('\nSolr is not currently running! Start Solr and try again.\n')
    log.fatal('Solr is not currently running! Start Solr and try again.')
    exit()
if not solr_utils.core_check():
    print(
        f'Solr core {SOLR_COLLECTION} does not exist. Add a core using "bin/solr create -c {{collection_name}}".')
    log.fatal(
        f'Solr core {SOLR_COLLECTION} does not exist. Add a core using "bin/solr create -c {{collection_name}}".')
    exit()

ds_status = defaultdict(list)


def create_parser():
    """
    Creates command line argument parser

    Returns:
        parser (ArgumentParser): the ArgumentParser object
    """
    parser = argparse.ArgumentParser()

    parser.add_argument('--grids_to_solr', default=False, action='store_true',
                        help='updates Solr with grids in grids_config')

    parser.add_argument('--single_processing', default=False, action='store_true',
                        help='turns off the use of multiprocessing during transformation')

    parser.add_argument('--multiprocesses', type=int, choices=range(1, cpu_count()+1),
                        default=int(cpu_count()/2), metavar=f'[1, {cpu_count()}]',
                        help=f'sets the number of multiprocesses used during transformation with a \
                            system max of {cpu_count()} with default set to half of system max')

    parser.add_argument('--harvested_entry_validation', default=False, action='store_true',
                        help='verifies each Solr granule entry points to a valid file.')

    parser.add_argument('--wipe_transformations', default=False, action='store_true',
                        help='deletes transformations with version number different than what is \
                            currently in transformation_config')

    parser.add_argument('--grids_to_use', default=False, nargs='*',
                        help='Names of grids to use during the pipeline')

    return parser


def print_statuses():
    print('\n=========================================================')
    print(
        '=================== \033[36mPrinting statuses\033[0m ===================')
    print('=========================================================')

    for ds, status_list in ds_status.items():
        print(f'\033[93mPipeline status for {ds}\033[0m:')
        for msg in status_list:
            if 'success' in msg:
                print(f'\t\033[92m{msg}\033[0m')
            else:
                print(f'\t\033[91m{msg}\033[0m')


def run_harvester(datasets, output_dir, grids_to_use):
    print('\n=========================================================')
    print(
        '================== \033[36mRunning harvesters\033[0m ===================')
    print('=========================================================\n')
    for ds in datasets:
        try:
            print(f'\033[93mRunning harvester for {ds}\033[0m')
            print('=========================================================')

            with open(Path(f'conf/ds_configs/{ds}.yaml'), 'r') as stream:
                config = yaml.load(stream, yaml.Loader)

            try:
                harvester_type = config['harvester_type']
            except:
                log.exception(
                    f'Harvester type missing from {ds} config. Exiting.')
                exit()
            if harvester_type == 'cmr':
                from harvesters.cmr_harvester import harvester
            elif harvester_type == 'podaac':
                from harvesters.podaac_harvester import harvester
            elif harvester_type == 'osisaf_ftp':
                from harvesters.osisaf_ftp_harvester import harvester
            elif harvester_type == 'nsidc_ftp':
                from harvesters.nsidc_ftp_harvester import harvester
            elif harvester_type == 'ifremer_ftp':
                from harvesters.ifremer_ftp_harvester import harvester
            else:
                print(f'{harvester_type} is not a supported harvester type.')
                log.exception(
                    f'{harvester_type} is not a supported harvester type.')
                exit()

            status = harvester(config, output_dir, grids_to_use)
            ds_status[ds].append(status)
            log.info(f'{ds} harvesting complete. {status}')
            print('\033[92mHarvest successful\033[0m')
        except Exception as e:
            ds_status[ds].append('Harvesting encountered error.')
            print(e)
            log.exception(f'{ds} harvesting failed. {e}')
            print('\033[91mHarvesting failed\033[0m')
        print('=========================================================')


def run_transformation(datasets, output_dir, multiprocessing, user_cpus, wipe, grids_to_use):
    print('\n=========================================================')
    print(
        '=============== \033[36mRunning transformations\033[0m =================')
    print('=========================================================\n')
    for ds in datasets:
        try:
            print(f'\033[93mRunning transformation for {ds}\033[0m')
            print('=========================================================')

            with open(Path(f'conf/ds_configs/{ds}.yaml'), 'r') as stream:
                config = yaml.load(stream, yaml.Loader)

            status = grid_transformation_local.main(config,
                                                    output_dir,
                                                    multiprocessing,
                                                    user_cpus,
                                                    wipe,
                                                    grids_to_use)
            ds_status[ds].append(status)

            log.info(f'{ds} transformation complete. {status}')
            print('\033[92mTransformation successful\033[0m')
        except:
            ds_status[ds].append('Transformation encountered error.')
            log.exception(f'{ds} transformation failed.')
            print('\033[91mTransformation failed\033[0m')
        print('=========================================================')


def run_aggregation(datasets, output_dir, grids_to_use):
    print('\n=========================================================')
    print(
        '================ \033[36mRunning aggregations\033[0m ===================')
    print('=========================================================\n')
    for ds in datasets:
        try:
            print(f'\033[93mRunning aggregation for {ds}\033[0m')
            print('=========================================================')

            with open(Path(f'conf/ds_configs/{ds}.yaml'), 'r') as stream:
                config = yaml.load(stream, yaml.Loader)

            status = aggregation(output_dir, config, grids_to_use)
            ds_status[ds].append(status)

            log.info(f'{ds} aggregation complete. {status}')
            print('\033[92mAggregation successful\033[0m')
        except Exception as e:
            ds_status[ds].append('Aggregation encountered error.')
            log.info(f'{ds} aggregation failed: {e}')
            print('\033[91mAggregation failed\033[0m')
        print('=========================================================')


if __name__ == '__main__':
    parser = create_parser()
    args = parser.parse_args()

    print('\n=================================================')
    print('========== ECCO PREPROCESSING PIPELINE ==========')
    print('=================================================')

    # ------------------- Harvested Entry Validation -------------------
    if args.harvested_entry_validation:
        solr_utils.validate_granules()

    # ------------------- Grids to Use -------------------
    if isinstance(args.grids_to_use, list):
        grids_to_use = args.grids_to_use
        verify_grids = True
    else:
        grids_to_use = []
        verify_grids = False

    # ------------------- Grids to Solr -------------------
    if args.grids_to_solr or verify_grids or solr_utils.check_grids():
        try:
            print(f'\n\033[93mRunning grids_to_solr\033[0m')
            print('=========================================================')
            grids_not_in_solr = []
            grids_not_in_solr = grids_to_solr.main(grids_to_use, verify_grids)

            if grids_not_in_solr:
                for name in grids_not_in_solr:
                    print(
                        f'Grid "{name}" not in Solr. Ensure it\'s file name is present in grids_config.yaml and run pipeline with the --grids_to_solr argument')
                exit()
            log.debug('Successfully updated grids on Solr.')
            print('\033[92mgrids_to_solr successful\033[0m')
        except Exception as e:
            print('\033[91mgrids_to_solr failed\033[0m')
            log.exception(e)
        print('=========================================================')

    # ------------------- Multiprocessing -------------------
    multiprocessing = not args.single_processing
    user_cpus = args.multiprocesses

    if multiprocessing:
        print(f'Using {user_cpus} processes for multiprocess transformations')
    else:
        print('Using single process transformations')

    # ------------------- Run pipeline -------------------
    while True:
        print('\n------------- OPTIONS -------------')
        print('1) Run all')
        print('2) Harvesters only')
        print('3) Up to aggregation')
        print('4) Dataset input')
        chosen_option = input('Enter option number: ')

        if chosen_option in ['1', '2', '3', '4']:
            break
        else:
            print(
                f'Unknown option entered, "{chosen_option}", please enter a valid option\n'
            )

    datasets = [os.path.splitext(ds)[0] for ds in os.listdir(
        'conf/ds_configs') if ds != '.DS_Store' and 'tpl' not in ds]
    datasets.sort()

    wipe = args.wipe_transformations

    # Run all
    if chosen_option == '1':
        for ds in datasets:
            run_harvester([ds], OUTPUT_DIR, grids_to_use)
            run_transformation([ds], OUTPUT_DIR, multiprocessing,
                               user_cpus, wipe, grids_to_use)
            run_aggregation([ds], OUTPUT_DIR, grids_to_use)

    # Run harvester
    elif chosen_option == '2':
        run_harvester(datasets, OUTPUT_DIR, grids_to_use)

    # Run up through transformation
    elif chosen_option == '3':
        for ds in datasets:
            run_harvester([ds], OUTPUT_DIR, grids_to_use)
            run_transformation([ds], OUTPUT_DIR, multiprocessing,
                               user_cpus, wipe, grids_to_use)

    # Manually enter dataset and pipeline step(s)
    elif chosen_option == '4':
        ds_dict = {i: ds for i, ds in enumerate(datasets, start=1)}
        while True:
            print(f'\nAvailable datasets:\n')
            for i, dataset in ds_dict.items():
                print(f'{i}) {dataset}')
            ds_index = input('\nEnter dataset number: ')

            if not ds_index.isdigit() or int(ds_index) not in range(1, len(datasets)+1):
                print(
                    f'Invalid dataset, "{ds_index}", please enter a valid selection')
            else:
                break

        wanted_ds = ds_dict[int(ds_index)]
        print(f'\nUsing {wanted_ds} dataset')

        steps = ['harvest', 'transform', 'aggregate',
                 'harvest and transform', 'transform and aggregate', 'all']
        steps_dict = {i: step for i, step in enumerate(steps, start=1)}
        while True:
            print(f'\nAvailable steps:\n')
            for i, step in steps_dict.items():
                print(f'{i}) {step}')
            steps_index = input('\nEnter pipeline step(s) number: ')

            if not steps_index.isdigit() or int(steps_index) not in range(1, len(steps)+1):
                print(
                    f'Invalid step(s), "{steps_index}", please enter a valid selection')
            else:
                break

        wanted_steps = steps_dict[int(steps_index)]

        if 'harvest' in wanted_steps:
            run_harvester([wanted_ds], OUTPUT_DIR, grids_to_use)
        if 'transform' in wanted_steps:
            run_transformation([wanted_ds], OUTPUT_DIR,
                               multiprocessing, user_cpus, wipe, grids_to_use)
        if 'aggregate' in wanted_steps:
            run_aggregation([wanted_ds], OUTPUT_DIR, grids_to_use)
        if wanted_steps == 'all':
            run_harvester([wanted_ds], OUTPUT_DIR, grids_to_use)
            run_transformation([wanted_ds], OUTPUT_DIR,
                               multiprocessing, user_cpus, wipe, grids_to_use)
            run_aggregation([wanted_ds], OUTPUT_DIR, grids_to_use)

    print_statuses()
