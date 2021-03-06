import logging
import logging.config
import os
from datetime import datetime
from urllib.request import urlcleanup, urlopen, urlretrieve
from xml.etree.ElementTree import parse

import numpy as np
import requests
import xarray as xr
from utils import file_utils, solr_utils

logging.config.fileConfig('logs/log.ini', disable_existing_loggers=False)
log = logging.getLogger(__name__)


def md5_check(local_fp, link):
    md5_url = f'{link}.md5'
    r = requests.get(md5_url)
    expected_md5 = r.content.decode("utf-8").split(' ')[0]
    local_md5 = file_utils.md5(local_fp)

    return local_md5, expected_md5


def granule_update_check(docs, newfile, mod_date_time, time_format):
    key = newfile.replace('.NRT', '')

    # Granule hasn't been harvested yet
    if key not in docs.keys():
        return True

    entry = docs[key]

    # Granule failed harvesting previously
    if not entry['harvest_success_b']:
        return True

    # Granule has been updated since last harvest
    if datetime.strptime(entry['download_time_dt'], time_format) <= mod_date_time:
        return True

    # Granule is replacing NRT version
    if '.NRT' in entry['filename_s'] and '.NRT' not in newfile:
        return True

    # Granule is up to date
    return False


def harvester(config, output_path, grids_to_use=[]):
    """
    Pulls data files for PODAAC id and date range given in harvester_config.yaml.
    Creates (or updates) Solr entries for dataset, harvested granule, fields,
    and descendants.
    """

    # =====================================================
    # Read harvester_config.yaml and setup variables
    # =====================================================
    dataset_name = config['ds_name']
    date_regex = config['date_regex']
    aggregated = config['aggregated']
    start_time = config['start']
    end_time = config['end']
    host = config['host']
    podaac_id = config['podaac_id']
    data_time_scale = config['data_time_scale']

    if end_time == 'NOW':
        end_time = datetime.utcnow().strftime("%Y%m%dT%H:%M:%SZ")

    target_dir = f'{output_path}/{dataset_name}/harvested_granules/'
    # If target paths don't exist, make them
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    time_format = "%Y-%m-%dT%H:%M:%SZ"
    entries_for_solr = []
    last_success_item = {}
    start_times = []
    end_times = []
    chk_time = datetime.utcnow().strftime(time_format)
    now = datetime.utcnow()
    updating = False

    solr_utils.clean_solr(config, grids_to_use)
    print(f'Downloading {dataset_name} files to {target_dir}\n')

    # =====================================================
    # Pull existing entries from Solr
    # =====================================================
    docs = {}
    descendants_docs = {}

    # Query for existing harvested docs
    fq = ['type_s:granule', f'dataset_s:{dataset_name}']
    harvested_docs = solr_utils.solr_query(fq)

    # Dictionary of existing harvested docs
    # harvested doc filename (without NRT if applicable) : solr entry for that doc
    if len(harvested_docs) > 0:
        for doc in harvested_docs:
            docs[doc['filename_s'].replace('.NRT', '')] = doc

    # Query for existing descendants docs
    fq = ['type_s:descendants', f'dataset_s:{dataset_name}']
    existing_descendants_docs = solr_utils.solr_query(fq)

    # Dictionary of existing descendants docs
    # descendant doc date : solr entry for that doc
    if len(existing_descendants_docs) > 0:
        for doc in existing_descendants_docs:
            descendants_docs[doc['date_s']] = doc

    # =====================================================
    # Setup PODAAC loop variables
    # =====================================================
    url = f'{host}&datasetId={podaac_id}'
    if not aggregated:
        url += f'&endTime={end_time}&startTime={start_time}'

    namespace = {"podaac": "http://podaac.jpl.nasa.gov/opensearch/",
                 "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
                 "atom": "http://www.w3.org/2005/Atom",
                 "georss": "http://www.georss.org/georss",
                 "gml": "http://www.opengis.net/gml",
                 "dc": "http://purl.org/dc/terms/",
                 "time": "http://a9.com/-/opensearch/extensions/time/1.0/"}

    xml = parse(urlopen(url))
    items = xml.findall('{%(atom)s}entry' % namespace)

    # Parse through XML results, building dictionary of entry metadata
    # Necessary for handling NRT files
    items_dict = {}

    for elem in items:
        title = elem.find('atom:title', namespace).text
        try:
            updated = elem.find('atom:updated', namespace).text
        except:
            updated = chk_time
        start = elem.find('time:start', namespace).text
        end = elem.find('time:end', namespace).text
        try:
            url = elem.find('atom:link[@title="OPeNDAP URL"]',
                            namespace).attrib['href'][:-5]
        except:
            # A few of the 1980s AVHRR granules are missing the OPeNDAP URL. We can
            # generate it by using the http url.
            url = elem.find('atom:link[@title="HTTP URL"]',
                            namespace).attrib['href']
            url = url.replace('podaac-tools.jpl.nasa.gov/drive/files',
                              'podaac-opendap.jpl.nasa.gov/opendap')
        entry = {'title': title,
                 'updated': updated,
                 'start': start,
                 'end': end,
                 'url': url}

        items_dict[title] = entry

    granules_to_process = []

    for title, entry in items_dict.items():
        if '.NRT' in title:
            # Skip NRT if non-NRT is available
            if title.replace('.NRT', '') in items_dict.keys():
                continue
            else:
                granules_to_process.append(entry)
        else:
            granules_to_process.append(entry)

    for granule in granules_to_process:
        updating = False

        # Extract granule information from XML entry and attempt to download data file
        try:
            link = granule['url']

            newfile = link.split("/")[-1]

            # Skip granules of unrecognized file format
            if not any(extension in newfile for extension in ['.nc', '.bz2', '.gz']):
                continue

            # Extract start and end dates from XML entry
            date_start_str = f'{granule["start"][:10]}T00:00:00Z'
            date_end_str = f'{granule["end"][:10]}T00:00:00Z'

            # Remove nanoseconds from dates
            if len(date_start_str) > 19:
                date_start_str = date_start_str[:19] + 'Z'
            if len(date_end_str) > 19:
                date_end_str = date_end_str[:19] + 'Z'
            if len(granule['updated']) > 19:
                update_time = granule['updated'][:19] + 'Z'
            else:
                update_time = granule['updated']

            # Ignore granules with start time less than wanted start time
            # PODAAC can grab granule previous to start time if that granule's
            # end time is the same as the config file's start time
            if date_start_str.replace('-', '') < start_time and not aggregated:
                continue

            mod_time = update_time
            mod_date_time = datetime.strptime(mod_time, date_regex)

            # Granule metadata used for Solr harvested entries
            item = {}
            item['type_s'] = 'granule'
            item['dataset_s'] = dataset_name
            item['date_s'] = date_start_str
            item['filename_s'] = newfile
            item['source_s'] = link
            item['modified_time_dt'] = mod_date_time.strftime(time_format)

            if newfile.replace('.NRT', '') in docs.keys():
                item['id'] = docs[newfile.replace('.NRT', '')]['id']

            # Granule metadata used for initializing Solr descendants entries
            descendants_item = {}
            descendants_item['type_s'] = 'descendants'
            descendants_item['dataset_s'] = dataset_name
            descendants_item['date_s'] = date_start_str
            descendants_item['filename_s'] = newfile
            descendants_item['source_s'] = link

            updating = granule_update_check(
                docs, newfile, mod_date_time, time_format)

            # If updating, download file if necessary
            if updating:
                year = date_start_str[:4]
                local_fp = f'{target_dir}{year}/{newfile}'

                if not os.path.exists(f'{target_dir}{year}'):
                    os.makedirs(f'{target_dir}{year}')

                # If file doesn't exist locally, download it
                if not os.path.exists(local_fp):
                    print(f' - Downloading {newfile} to {local_fp}')
                    if aggregated:
                        print(
                            f'    - {newfile} is aggregated. Downloading may be slow.')

                    urlcleanup()
                    urlretrieve(link, local_fp)

                    # BZ2 compression results in differet MD5 values
                    if 'bz2' not in local_fp:
                        local_md5, expected_md5 = md5_check(local_fp, link)
                        if local_md5 != expected_md5:
                            raise ValueError(
                                f'Downloaded file MD5 value ({local_md5}) does not match expected value from server ({expected_md5}).')
                    else:
                        response = requests.head(link)
                        if 'Content-Length' in response.headers.keys():
                            expected_size = int(
                                response.headers['Content-Length'])
                            actual_size = os.path.getsize(local_fp)
                            if actual_size != expected_size:
                                raise ValueError(
                                    f'Downloaded file size ({actual_size}) does not match expected size from server ({expected_size}).')
                        else:
                            print(
                                'Content-Length header unavailable. Unable to verify successful file download.')

                # If file exists locally, but is out of date, download it
                elif datetime.fromtimestamp(os.path.getmtime(local_fp)) <= mod_date_time:
                    print(
                        f' - Updating {newfile} and downloading to {local_fp}')
                    if aggregated:
                        print(
                            f'    - {newfile} is aggregated. Downloading may be slow.')

                    urlcleanup()
                    urlretrieve(link, local_fp)

                    # BZ2 compression results in differet MD5 values
                    if 'bz2' not in local_fp:
                        local_md5, expected_md5 = md5_check(local_fp, link)
                        if local_md5 != expected_md5:
                            raise ValueError(
                                f'Downloaded file MD5 value ({local_md5}) does not match expected value from server ({expected_md5}).')
                    else:
                        response = requests.head(link)
                        if 'Content-Length' in response.headers.keys():
                            expected_size = int(
                                response.headers['Content-Length'])
                            actual_size = os.path.getsize(local_fp)
                            if actual_size != expected_size:
                                raise ValueError(
                                    f'Downloaded file size ({actual_size}) does not match expected size from server ({expected_size}).')
                        else:
                            print(
                                'Content-Length header unavailable. Unable to verify successful file download.')

                else:
                    print(f' - {newfile} already downloaded and up to date')

                # Create checksum for file
                item['checksum_s'] = file_utils.md5(local_fp)
                item['pre_transformation_file_path_s'] = local_fp
                item['harvest_success_b'] = True
                item['file_size_l'] = os.path.getsize(local_fp)

                # =====================================================
                # Handling data in aggregated form
                # =====================================================
                if aggregated:
                    # Aggregated file has already been downloaded
                    # Must extract individual granule slices
                    print(
                        f' - Extracting individual data granules from aggregated data file')

                    # Remove old outdated aggregated file from disk
                    for f in os.listdir(f'{target_dir}{year}/'):
                        if str(f) != str(newfile):
                            os.remove(f'{target_dir}{year}/{f}')

                    ds = xr.open_dataset(local_fp)

                    # List comprehension extracting times within desired date range
                    ds_times = [
                        time for time
                        in np.datetime_as_string(ds.time.values)
                        if start_time[:9] <= time.replace('-', '')[:9] <= end_time[:9]
                    ]

                    for time in ds_times:
                        new_ds = ds.sel(time=time)

                        if data_time_scale.upper() == 'MONTHLY':
                            if not time[7:9] == '01':
                                new_start = f'{time[0:8]}01T00:00:00.000000000'
                                print('NS: ', new_start, 'T: ', time)
                                time = new_start
                        year = time[:4]

                        file_name = f'{dataset_name}_{time.replace("-","")[:8]}.nc'
                        local_fp = f'{target_dir}{year}/{file_name}'
                        time_s = f'{time[:-10]}Z'

                        # Granule metadata used for Solr harvested entries
                        item = {}
                        item['type_s'] = 'granule'
                        item['date_s'] = time_s
                        item['dataset_s'] = dataset_name
                        item['filename_s'] = file_name
                        item['source_s'] = link
                        item['modified_time_dt'] = mod_date_time.strftime(
                            time_format)
                        item['download_time_dt'] = chk_time

                        # Granule metadata used for initializing Solr descendants entries
                        descendants_item = {}
                        descendants_item['type_s'] = 'descendants'
                        descendants_item['dataset_s'] = item['dataset_s']
                        descendants_item['date_s'] = item["date_s"]
                        descendants_item['source_s'] = item['source_s']

                        if not os.path.exists(f'{target_dir}{year}'):
                            os.makedirs(f'{target_dir}{year}')

                        try:
                            # Save slice as NetCDF
                            new_ds.to_netcdf(path=local_fp)

                            # Create checksum for file
                            item['checksum_s'] = file_utils.md5(local_fp)
                            item['pre_transformation_file_path_s'] = local_fp
                            item['harvest_success_b'] = True
                            item['file_size_l'] = os.path.getsize(local_fp)
                        except:
                            print(f'    - {file_name} failed to save')
                            item['harvest_success_b'] = False
                            item['pre_transformation_file_path_s'] = ''
                            item['file_size_l'] = 0
                            item['checksum_s'] = ''

                        # Query for existing granule in Solr in order to update it
                        fq = ['type_s:granule', f'dataset_s:{dataset_name}',
                              f'date_s:{time_s[:10]}*']
                        granule = solr_utils.solr_query(fq)

                        if granule:
                            item['id'] = granule[0]['id']

                        if time_s in descendants_docs.keys():
                            descendants_item['id'] = descendants_docs[time_s]['id']

                        entries_for_solr.append(item)
                        entries_for_solr.append(descendants_item)

                        start_times.append(datetime.strptime(
                            time[:-3], '%Y-%m-%dT%H:%M:%S.%f'))
                        end_times.append(datetime.strptime(
                            time[:-3], '%Y-%m-%dT%H:%M:%S.%f'))

                        if item['harvest_success_b']:
                            last_success_item = item

            else:
                print(f' - {newfile} already downloaded and up to date')

        except Exception as e:
            print(f'    - {e}')
            log.exception(
                f'{dataset_name} harvesting error! {newfile} failed to download.')
            if updating:

                print(f'    - {newfile} failed to download')

                item['harvest_success_b'] = False
                item['pre_transformation_file_path_s'] = ''
                item['file_size_l'] = 0

        if updating:
            item['download_time_dt'] = chk_time

            if date_start_str in descendants_docs.keys():
                descendants_item['id'] = descendants_docs[date_start_str]['id']

            descendants_item['harvest_success_b'] = item['harvest_success_b']
            descendants_item['pre_transformation_file_path_s'] = item['pre_transformation_file_path_s']

            if not aggregated:
                entries_for_solr.append(item)
                entries_for_solr.append(descendants_item)

                start_times.append(datetime.strptime(
                    date_start_str, date_regex))
                end_times.append(datetime.strptime(
                    date_end_str, date_regex))

                if item['harvest_success_b']:
                    last_success_item = item

    # Only update Solr harvested entries if there are fresh downloads
    if entries_for_solr:
        # Update Solr with downloaded granule metadata entries
        r = solr_utils.solr_update(entries_for_solr, r=True)
        if r.status_code == 200:
            print('Successfully created or updated Solr harvested documents')
        else:
            print('Failed to create Solr harvested documents')

    # Query for Solr failed harvest documents
    fq = ['type_s:granule', f'dataset_s:{dataset_name}',
          f'harvest_success_b:false']
    failed_harvesting = solr_utils.solr_query(fq)

    # Query for Solr successful harvest documents
    fq = ['type_s:granule',
          f'dataset_s:{dataset_name}', f'harvest_success_b:true']
    successful_harvesting = solr_utils.solr_query(fq)

    harvest_status = f'All granules successfully harvested'

    if not successful_harvesting:
        harvest_status = f'No usable granules harvested (either all failed or no data collected)'
    elif failed_harvesting:
        harvest_status = f'{len(failed_harvesting)} harvested granules failed'

    overall_start = min(start_times) if start_times else None
    overall_end = max(end_times) if end_times else None

    # Query for Solr Dataset-level Document
    fq = ['type_s:dataset', f'dataset_s:{dataset_name}']
    dataset_query = solr_utils.solr_query(fq)

    # If dataset entry exists on Solr
    update = (len(dataset_query) == 1)

    # =====================================================
    # Solr dataset entry
    # =====================================================
    if not update:
        # -----------------------------------------------------
        # Create Solr dataset entry
        # -----------------------------------------------------
        ds_meta = {}
        ds_meta['type_s'] = 'dataset'
        ds_meta['dataset_s'] = dataset_name
        ds_meta['short_name_s'] = config['original_dataset_short_name']
        ds_meta['source_s'] = f'{host}&datasetId={podaac_id}'
        ds_meta['data_time_scale_s'] = config['data_time_scale']
        ds_meta['date_format_s'] = config['date_format']
        ds_meta['last_checked_dt'] = chk_time
        ds_meta['original_dataset_title_s'] = config['original_dataset_title']
        ds_meta['original_dataset_short_name_s'] = config['original_dataset_short_name']
        ds_meta['original_dataset_url_s'] = config['original_dataset_url']
        ds_meta['original_dataset_reference_s'] = config['original_dataset_reference']
        ds_meta['original_dataset_doi_s'] = config['original_dataset_doi']

        # Only include start_date and end_date if there was at least one successful download
        if overall_start != None:
            ds_meta['start_date_dt'] = overall_start.strftime(time_format)
            ds_meta['end_date_dt'] = overall_end.strftime(time_format)

        # Only include last_download_dt if there was at least one successful download
        if last_success_item:
            ds_meta['last_download_dt'] = last_success_item['download_time_dt']

        ds_meta['harvest_status_s'] = harvest_status

        # Update Solr with dataset metadata
        r = solr_utils.solr_update([ds_meta], r=True)

        if r.status_code == 200:
            print('Successfully created Solr dataset document')
        else:
            print('Failed to create Solr dataset document')

        # If the dataset entry needs to be created, so do the field entries

        # -----------------------------------------------------
        # Create Solr dataset field entries
        # -----------------------------------------------------

        # Query for Solr field documents
        fq = ['type_s:field', f'dataset_s:{dataset_name}']
        field_query = solr_utils.solr_query(fq)

        body = []
        for field in config['fields']:
            field_obj = {}
            field_obj['type_s'] = {'set': 'field'}
            field_obj['dataset_s'] = {'set': dataset_name}
            field_obj['name_s'] = {'set': field['name']}
            field_obj['long_name_s'] = {'set': field['long_name']}
            field_obj['standard_name_s'] = {'set': field['standard_name']}
            field_obj['units_s'] = {'set': field['units']}

            for solr_field in field_query:
                if field['name'] == solr_field['name_s']:
                    field_obj['id'] = {'set': solr_field['id']}

            body.append(field_obj)

        if body:
            # Update Solr with dataset fields metadata
            r = solr_utils.solr_update(body, r=True)

            if r.status_code == 200:
                print('Successfully created Solr field documents')
            else:
                print('Failed to create Solr field documents')

    # if dataset entry exists, update download time, converage start date, coverage end date
    else:
        # -----------------------------------------------------
        # Update Solr dataset entry
        # -----------------------------------------------------
        dataset_metadata = dataset_query[0]

        # Query for dates of all harvested docs
        fq = [f'dataset_s:{dataset_name}',
              'type_s:granule', 'harvest_success_b:true']
        dates_query = solr_utils.solr_query(fq, fl='date_s')
        dates = [x['date_s'] for x in dates_query]

        # Build update document body
        update_doc = {}
        update_doc['id'] = dataset_metadata['id']
        update_doc['last_checked_dt'] = {"set": chk_time}
        if dates:
            update_doc['start_date_dt'] = {"set": min(dates)}
            update_doc['end_date_dt'] = {"set": max(dates)}

        if entries_for_solr:
            update_doc['harvest_status_s'] = {"set": harvest_status}

            if 'download_time_dt' in last_success_item.keys():
                update_doc['last_download_dt'] = {
                    "set": last_success_item['download_time_dt']}

        # Update Solr with modified dataset entry
        r = solr_utils.solr_update([update_doc], r=True)

        if r.status_code == 200:
            print('Successfully updated Solr dataset document\n')
        else:
            print('Failed to update Solr dataset document\n')

    return harvest_status
