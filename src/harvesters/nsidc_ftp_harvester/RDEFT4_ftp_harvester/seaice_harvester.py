from __future__ import print_function
from xml.etree.ElementTree import parse
import datetime
# from datetime import datetime, timedelta
import sys
from os import path
from pathlib import Path
import os
import re
# from urllib.request import urlopen, urlcleanup, urlretrieve
import gzip
import shutil
import hashlib
import requests
import json
import yaml
from ftplib import FTP
from dateutil import parser
import numpy as np

import base64
import itertools
import netrc
import ssl
try:
    from urllib.parse import urlparse
    from urllib.request import urlopen, Request, build_opener, HTTPCookieProcessor
    from urllib.error import HTTPError, URLError
except ImportError:
    from urlparse import urlparse
    from urllib2 import urlopen, Request, HTTPError, URLError, build_opener, HTTPCookieProcessor


CMR_URL = 'https://cmr.earthdata.nasa.gov'
URS_URL = 'https://urs.earthdata.nasa.gov'
CMR_PAGE_SIZE = 2000
CMR_FILE_URL = ('{0}/search/granules.json?provider=NSIDC_ECS'
                '&sort_key[]=start_date&sort_key[]=producer_granule_id'
                '&scroll=true&page_size={1}'.format(CMR_URL, CMR_PAGE_SIZE))


def get_credentials(url):
    """Get user credentials from .netrc or prompt for input."""
    credentials = None
    errprefix = ''
    try:
        info = netrc.netrc()
        username, account, password = info.authenticators(
            urlparse(URS_URL).hostname)
        errprefix = 'netrc error: '
    except Exception as e:
        if (not ('No such file' in str(e))):
            print('netrc error: {0}'.format(str(e)))
        username = None
        password = None

    while not credentials:
        if not username:
            username = 'ecco_access'  # hardcoded username
            password = 'ECCOAccess1'  # hardcoded password
        credentials = '{0}:{1}'.format(username, password)
        credentials = base64.b64encode(
            credentials.encode('ascii')).decode('ascii')

        if url:
            try:
                req = Request(url)
                req.add_header('Authorization',
                               'Basic {0}'.format(credentials))
                opener = build_opener(HTTPCookieProcessor())
                opener.open(req)
            except HTTPError:
                print(errprefix + 'Incorrect username or password')
                errprefix = ''
                credentials = None
                username = None
                password = None

    return credentials


def build_version_query_params(version):
    desired_pad_length = 3
    if len(version) > desired_pad_length:
        print('Version string too long: "{0}"'.format(version))
        quit()

    version = str(int(version))  # Strip off any leading zeros
    query_params = ''

    while len(version) <= desired_pad_length:
        padded_version = version.zfill(desired_pad_length)
        query_params += '&version={0}'.format(padded_version)
        desired_pad_length -= 1
    return query_params


def build_cmr_query_url(short_name, version, time_start, time_end,
                        bounding_box=None, polygon=None,
                        filename_filter=None):
    params = '&short_name={0}'.format(short_name)
    params += build_version_query_params(version)
    params += '&temporal[]={0},{1}'.format(time_start, time_end)
    if polygon:
        params += '&polygon={0}'.format(polygon)
    elif bounding_box:
        params += '&bounding_box={0}'.format(bounding_box)
    if filename_filter:
        option = '&options[producer_granule_id][pattern]=true'
        params += '&producer_granule_id[]={0}{1}'.format(
            filename_filter, option)
    return CMR_FILE_URL + params


def cmr_download(urls):
    """Download files from list of urls."""
    if not urls:
        return

    url_count = len(urls)
    print('Downloading {0} files...'.format(url_count))
    credentials = None

    for index, url in enumerate(urls, start=1):
        if not credentials and urlparse(url).scheme == 'https':
            credentials = get_credentials(url)

        filename = url.split('/')[-1]
        print('{0}/{1}: {2}'.format(str(index).zfill(len(str(url_count))),
                                    url_count,
                                    filename))

        try:
            # In Python 3 we could eliminate the opener and just do 2 lines:
            # resp = requests.get(url, auth=(username, password))
            # open(filename, 'wb').write(resp.content)
            req = Request(url)
            if credentials:
                req.add_header('Authorization',
                               'Basic {0}'.format(credentials))
            opener = build_opener(HTTPCookieProcessor())
            data = opener.open(req).read()
            open(filename, 'wb').write(data)
        except HTTPError as e:
            print('HTTP error {0}, {1}'.format(e.code, e.reason))
        except URLError as e:
            print('URL error: {0}'.format(e.reason))
        except IOError:
            raise
        except KeyboardInterrupt:
            quit()


def cmr_filter_urls(search_results):
    """Select only the desired data files from CMR response."""
    if 'feed' not in search_results or 'entry' not in search_results['feed']:
        return []

    entries = [e['links']
               for e in search_results['feed']['entry']
               if 'links' in e]
    # Flatten "entries" to a simple list of links
    links = list(itertools.chain(*entries))

    urls = []
    unique_filenames = set()
    for link in links:
        if 'href' not in link:
            # Exclude links with nothing to download
            continue
        if 'inherited' in link and link['inherited'] is True:
            # Why are we excluding these links?
            continue
        if 'rel' in link and 'data#' not in link['rel']:
            # Exclude links which are not classified by CMR as "data" or "metadata"
            continue

        if 'title' in link and 'opendap' in link['title'].lower():
            # Exclude OPeNDAP links--they are responsible for many duplicates
            # This is a hack; when the metadata is updated to properly identify
            # non-datapool links, we should be able to do this in a non-hack way
            continue

        filename = link['href'].split('/')[-1]
        if filename in unique_filenames:
            # Exclude links with duplicate filenames (they would overwrite)
            continue
        unique_filenames.add(filename)

        urls.append(link['href'])

    return urls


def cmr_search(short_name, version, time_start, time_end,
               bounding_box='', polygon='', filename_filter=''):
    """Perform a scrolling CMR query for files matching input criteria."""
    cmr_query_url = build_cmr_query_url(short_name=short_name, version=version,
                                        time_start=time_start, time_end=time_end,
                                        bounding_box=bounding_box,
                                        polygon=polygon, filename_filter=filename_filter)
    print('Querying for data:\n\t{0}\n'.format(cmr_query_url))

    cmr_scroll_id = None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        urls = []
        while True:
            req = Request(cmr_query_url)
            if cmr_scroll_id:
                req.add_header('cmr-scroll-id', cmr_scroll_id)
            response = urlopen(req, context=ctx)
            if not cmr_scroll_id:
                # Python 2 and 3 have different case for the http headers
                headers = {k.lower(): v for k, v in dict(
                    response.info()).items()}
                cmr_scroll_id = headers['cmr-scroll-id']
                hits = int(headers['cmr-hits'])
                if hits > 0:
                    print('Found {0} matches.'.format(hits))
                else:
                    print('Found no matches.')
            search_page = response.read()
            search_page = json.loads(search_page.decode('utf-8'))
            url_scroll_results = cmr_filter_urls(search_page)
            if not url_scroll_results:
                break
            if hits > CMR_PAGE_SIZE:
                print('.', end='')
                sys.stdout.flush()
            urls += url_scroll_results

        if hits > CMR_PAGE_SIZE:
            print()
        return urls
    except KeyboardInterrupt:
        quit()


def md5(fname):
    hash_md5 = hashlib.md5()
    with open(fname, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def getdate(regex, fname):
    ex = re.compile(regex)
    match = re.search(ex, fname)
    date = match.group()
    return date


def solr_query(config, solr_host, fq, solr_collection_name):
    # solr_collection_name = config['solr_collection_name']

    getVars = {'q': '*:*',
               'fq': fq,
               'rows': 300000}

    url = f'{solr_host}{solr_collection_name}/select?'
    response = requests.get(url, params=getVars)
    return response.json()['response']['docs']


def solr_update(config, solr_host, update_body, solr_collection_name, r=False):
    # solr_host = config['solr_host']
    # solr_collection_name = config['solr_collection_name']

    url = solr_host + solr_collection_name + '/update?commit=true'

    if r:
        return requests.post(url, json=update_body)
    else:
        requests.post(url, json=update_body)


def seaice_harvester(config_path='', output_path='', s3=None, on_aws=False, solr_info=''):
    # =====================================================
    # Read configurations from YAML file
    # =====================================================
    if not config_path:
        print('No path for configuration file. Can not run harvester.')
        return

    with open(config_path, "r") as stream:
        config = yaml.load(stream, yaml.Loader)

    # =====================================================
    # Code to import ecco utils locally...
    # =====================================================
    generalized_functions_path = Path(
        f'{Path(__file__).resolve().parents[5]}/ECCO-ACCESS/ecco-cloud-utils/')
    sys.path.append(str(generalized_functions_path))
    import ecco_cloud_utils as ea  # pylint: disable=import-error

    # =====================================================
    # Setup AWS Target Bucket
    # =====================================================
    if on_aws:
        target_bucket_name = config['target_bucket_name']
        target_bucket = s3.Bucket(target_bucket_name)

    # =====================================================
    # Download raw data files
    # =====================================================
    dataset_name = config['ds_name']
    target_dir = f'{output_path}{dataset_name}/harvested_granules/'
    folder = f'/tmp/{dataset_name}/'
    data_time_scale = config['data_time_scale']

    short_name = 'RDEFT4'
    if solr_info:
        solr_host = solr_info['solr_url']
        solr_collection_name = solr_info['solr_collection_name']
    else:
        solr_host = config['solr_host_local']
        solr_collection_name = config['solr_collection_name']
    version = '1'

    if not on_aws:
        print(f'Downloading {dataset_name} files to {target_dir}\n')
    else:
        print(
            f'Downloading {dataset_name} files and uploading to {target_bucket_name}/{dataset_name}\n')

    print("======downloading files========")

    # if target path doesn't exist, make it
    # if tmp folder for downloaded files doesn't exist, create it in temp lambda storage
    if not os.path.exists(folder):
        os.makedirs(folder)

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    # get old data if exists
    docs = {}
    descendants_docs = {}

    fq = ['type_s:harvested', f'dataset_s:{config["ds_name"]}']
    query_docs = solr_query(config, solr_host, fq, solr_collection_name)

    if len(query_docs) > 0:
        for doc in query_docs:
            docs[doc['filename_s']] = doc

    fq = ['type_s:dataset', f'dataset_s:{config["ds_name"]}']
    query_docs = solr_query(config, solr_host, fq, solr_collection_name)

    # Query for existing descendants docs
    fq = ['type_s:descendants', f'dataset_s:{dataset_name}']
    existing_descendants_docs = solr_query(config, solr_host, fq, solr_collection_name)

    if len(existing_descendants_docs) > 0:
        for doc in existing_descendants_docs:
            if 'hemisphere_s' in doc.keys() and doc['hemisphere_s']:
                key = (doc['date_s'], doc['hemisphere_s'])
            else:
                key = doc['date_s']
            descendants_docs[key] = doc

    # setup metadata
    meta = []
    item = {}
    last_success_item = {}
    start = []
    end = []
    chk_time = datetime.datetime.utcnow().strftime(config['date_regex'])
    now = datetime.datetime.utcnow()
    updating = False
    aws_upload = False

    start_year = config['start'][:4]
    end_year = config['end'][:4]
    years = np.arange(int(start_year), int(end_year) + 1)
    start_time = datetime.datetime.strptime(
        config['start'], config['date_regex'])
    end_time = datetime.datetime.strptime(config['end'], config['date_regex'])

    url_list = cmr_search(short_name, version, config['start'], config['end'])

    for year in years:

        iso_dates_at_end_of_month = []

        # pull one record per month
        for month in range(1, 13):
            # to find the last day of the month, we go up one month,
            # and back one day
            #   if Jan-Nov, then we'll go forward one month to Feb-Dec

            if month < 12:
                cur_mon_year = np.datetime64(
                    str(year) + '-' + str(month+1).zfill(2))
            # for december we go up one year, and set month to january
            else:
                cur_mon_year = np.datetime64(str(year+1) + '-' + str('01'))

            # then back one day
            last_day_of_month = cur_mon_year - np.timedelta64(1, 'D')

            iso_dates_at_end_of_month.append(
                (str(last_day_of_month)).replace('-', ''))

        url_dict = {}

        for file_date in iso_dates_at_end_of_month:
            end_of_month_url = [url for url in url_list if file_date in url]

            if end_of_month_url:
                url_dict[file_date] = end_of_month_url[0]

        for file_date, url in url_dict.items():

            # Date in filename is end date of 30 day period
            filename = url.split('/')[-1]

            date = getdate(config['regex'], filename)
            date_time = datetime.datetime.strptime(date, "%Y%m%d")
            new_date_format = f'{date[:4]}-{date[4:6]}-{date[6:]}T00:00:00Z'

            tb, _ = ea.make_time_bounds_from_ds64(np.datetime64(new_date_format) + np.timedelta64(1, 'D'), 'AVG_MON')
            new_date_format = f'{str(tb[0])[:10]}T00:00:00Z'
            year = new_date_format[:4]

            local_fp = f'{folder}{config["ds_name"]}_granule.nc' if on_aws else f'{target_dir}{year}/{filename}'

            if not os.path.exists(f'{target_dir}{year}/'):
                os.makedirs(f'{target_dir}{year}/')

            start.append(datetime.datetime.strptime(
                new_date_format, config['date_regex']))
            end.append(datetime.datetime.strptime(
                new_date_format, config['date_regex']))

            # check if file in download date range
            if (start_time <= date_time) and (end_time >= date_time):
                item = {}
                item['type_s'] = 'harvested'
                item['date_s'] = new_date_format
                item['dataset_s'] = config['ds_name']
                item['source_s'] = url

                # descendants metadta setup to be populated for each granule
                descendants_item = {}
                descendants_item['type_s'] = 'descendants'

                # Create or modify descendants entry in Solr
                descendants_item['dataset_s'] = item['dataset_s']
                descendants_item['date_s'] = item["date_s"]
                descendants_item['source_s'] = item['source_s']

                updating = False
                aws_upload = False

                try:

                    # TODO: find a way to get last modified (see line 436 as well)
                    # get last modified date
                    # timestamp = ftp.voidcmd("MDTM "+url)[4:]    # string
                    # time = parser.parse(timestamp)              # datetime object
                    # timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ") # string
                    # item['modified_time_dt'] = timestamp

                    # compare modified timestamp or if granule previously downloaded
                    # updating = (not newfile in docs.keys()) or (not docs[newfile]['harvest_success_b']) or (datetime.datetime.strptime(docs[newfile]['download_time_dt'], "%Y-%m-%dT%H:%M:%SZ") <= time)
                    updating = (not filename in docs.keys()) or (
                        not docs[filename]['harvest_success_b'])

                    if updating:
                        if not os.path.exists(local_fp):

                            print(f' - Downloading {filename} to {local_fp}')

                            credentials = get_credentials(url)
                            req = Request(url)
                            req.add_header('Authorization',
                                           'Basic {0}'.format(credentials))
                            opener = build_opener(HTTPCookieProcessor())
                            data = opener.open(req).read()
                            open(local_fp, 'wb').write(data)

                        # elif datetime.datetime.fromtimestamp(os.path.getmtime(local_fp)) <= time:
                        elif datetime.datetime.fromtimestamp(os.path.getmtime(local_fp)) <= parser.parse(file_date):

                            print(
                                f' - Updating {filename} and downloading to {local_fp}')

                            credentials = get_credentials(url)
                            req = Request(url)
                            req.add_header('Authorization',
                                           'Basic {0}'.format(credentials))
                            opener = build_opener(HTTPCookieProcessor())
                            data = opener.open(req).read()
                            open(local_fp, 'wb').write(data)

                        else:
                            print(
                                f' - {filename} already downloaded and up to date')

                        # calculate checksum and expected file size
                        item['checksum_s'] = md5(local_fp)

                        # =====================================================
                        # ### Push data to s3 bucket
                        # =====================================================
                        output_filename = config['ds_name'] + \
                            '/' + filename if on_aws else filename
                        item['pre_transformation_file_path_s'] = local_fp

                        if on_aws:
                            aws_upload = True
                            print("=========uploading file=========")
                            # print('uploading '+output_filename)
                            target_bucket.upload_file(
                                local_fp, output_filename)
                            item['pre_transformation_file_path_s'] = 's3://' + \
                                config['target_bucket_name'] + \
                                '/'+output_filename
                            print("======uploading file DONE=======")

                        item['harvest_success_b'] = True
                        item['filename_s'] = filename
                        item['file_size_l'] = os.path.getsize(local_fp)

                except Exception as e:
                    print('error', e)
                    if updating:
                        if aws_upload:
                            print("======aws upload unsuccessful=======")
                            item['message_s'] = 'aws upload unsuccessful'

                        else:
                            print(f'    - {filename} failed to download')

                        item['harvest_success_b'] = False
                        item['filename'] = ''
                        item['pre_transformation_file_path_s'] = ''
                        item['file_size_l'] = 0

                if updating:
                    item['download_time_dt'] = chk_time

                    # Update Solr entry using id if it exists

                    key = descendants_item['date_s']

                    if key in descendants_docs.keys():
                        descendants_item['id'] = descendants_docs[key]['id']

                    descendants_item['harvest_success_b'] = item['harvest_success_b']
                    descendants_item['pre_transformation_file_path_s'] = item['pre_transformation_file_path_s']
                    meta.append(descendants_item)

                    # add item to metadata json
                    meta.append(item)
                    # store meta for last successful download
                    last_success_item = item

    print(f'\nDownloading {dataset_name} complete\n')

    if meta:
        # post granule metadata documents for downloaded granules
        r = solr_update(config, solr_host, meta, solr_collection_name, r=True)

        if r.status_code == 200:
            print('Successfully created or updated Solr harvested documents')
        else:
            print('Failed to create Solr harvested documents')

    # Query for Solr failed harvest documents
    fq = ['type_s:harvested',
          f'dataset_s:{dataset_name}', f'harvest_success_b:false']
    failed_harvesting = solr_query(config, solr_host, fq, solr_collection_name)

    # Query for Solr successful harvest documents
    fq = ['type_s:harvested',
          f'dataset_s:{dataset_name}', f'harvest_success_b:true']
    successful_harvesting = solr_query(config, solr_host, fq, solr_collection_name)

    harvest_status = f'All granules successfully harvested'

    if not successful_harvesting:
        harvest_status = f'No usable granules harvested (either all failed or no data collected)'
    elif failed_harvesting:
        harvest_status = f'{len(failed_harvesting)} harvested granules failed'

    overall_start = min(start) if len(start) > 0 else None
    overall_end = max(end) if len(end) > 0 else None

    fq = ['type_s:dataset', 'dataset_s:'+config['ds_name']]
    docs = solr_query(config, solr_host, fq, solr_collection_name)

    update = (len(docs) == 1)

    # if no dataset-level entry in Solr, create one
    if not update:
        # TODO: THIS SECTION BELONGS WITH DATASET DISCOVERY
        # -----------------------------------------------------
        # Create Solr Dataset-level Document if doesn't exist
        # -----------------------------------------------------
        ds_meta = {}
        ds_meta['type_s'] = 'dataset'
        ds_meta['dataset_s'] = config['ds_name']
        ds_meta['short_name_s'] = config['original_dataset_short_name']
        ds_meta['source_s'] = url_list[0][:-30]
        ds_meta['data_time_scale_s'] = config['data_time_scale']
        ds_meta['date_format_s'] = config['date_format']
        ds_meta['last_checked_dt'] = chk_time
        ds_meta['original_dataset_title_s'] = config['original_dataset_title']
        ds_meta['original_dataset_short_name_s'] = config['original_dataset_short_name']
        ds_meta['original_dataset_url_s'] = config['original_dataset_url']
        ds_meta['original_dataset_reference_s'] = config['original_dataset_reference']
        ds_meta['original_dataset_doi_s'] = config['original_dataset_doi']
        if overall_start != None:
            ds_meta['start_date_dt'] = overall_start.strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            ds_meta['end_date_dt'] = overall_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        # if no ds entry yet and no qualifying downloads, still create ds entry without download time
        if updating:
            ds_meta['last_download_dt'] = last_success_item['download_time_dt']

        ds_meta['harvest_status_s'] = harvest_status

        body = []
        body.append(ds_meta)

        # Post document
        r = solr_update(config, solr_host, body, solr_collection_name, r=True)

        if r.status_code == 200:
            print('Successfully created Solr dataset document')
        else:
            print('Failed to create Solr dataset document')

        # TODO: update for changes in yaml (incinerate and rewrite)
        # modify updating variable to account for updates in config and then incinerate and rewrite
        # Query for Solr field documents
        fq = ['type_s:field', f'dataset_s:{dataset_name}']
        field_query = solr_query(config, solr_host, fq, solr_collection_name)

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

        # Update Solr with dataset fields metadata
        r = solr_update(config, solr_host, body, solr_collection_name, r=True)

        if r.status_code == 200:
            print('Successfully created Solr field documents')
        else:
            print('Failed to create Solr field documents')

    # if record exists, update download time, converage start date, coverage end date
    else:
        # =====================================================
        # Check start and end date coverage
        # =====================================================
        doc = docs[0]
        old_start = datetime.datetime.strptime(
            doc['start_date_dt'], "%Y-%m-%dT%H:%M:%SZ") if 'start_date_dt' in doc.keys() else None
        old_end = datetime.datetime.strptime(
            doc['end_date_dt'], "%Y-%m-%dT%H:%M:%SZ") if 'end_date_dt' in doc.keys() else None
        doc_id = doc['id']

        # build update document body
        update_doc = {}
        update_doc['id'] = doc_id
        update_doc['last_checked_dt'] = {"set": chk_time}
        update_doc['status_s'] = {"set": "harvested"}

        if updating:
            # only update to "harvested" if there is further preprocessing to do
            update_doc['harvest_status_s'] = harvest_status

            if len(meta) > 0 and 'download_time_dt' in last_success_item.keys():
                update_doc['last_download_dt'] = {
                    "set": last_success_item['download_time_dt']}
            if old_start == None or overall_start < old_start:
                update_doc['start_date_dt'] = {
                    "set": overall_start.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if old_end == None or overall_end > old_end:
                update_doc['end_date_dt'] = {
                    "set": overall_end.strftime("%Y-%m-%dT%H:%M:%SZ")}

        body = [update_doc]
        r = solr_update(config, solr_host, body, solr_collection_name, r=True)

        if r.status_code == 200:
            print('Successfully updated Solr dataset document\n')
        else:
            print('Failed to update Solr dataset document\n')
