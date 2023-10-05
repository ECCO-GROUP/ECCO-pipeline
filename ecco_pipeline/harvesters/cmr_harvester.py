import calendar
import logging
import os
from datetime import datetime, timedelta
from typing import Iterable

import numpy as np
import requests
import xarray as xr
from harvesters.enumeration.cmr_enumerator import cmr_search, CMR_Granule
from harvesters.granule import Granule
from harvesters.harvester import Harvester
from utils import file_utils, harvesting_utils


class CMR_Harvester(Harvester):
    
    def __init__(self, config):
        Harvester.__init__(self, config)
        if config['end'] == 'NOW':
            config['end'] = datetime.utcnow().strftime("%Y%m%dT%H:%M:%SZ")
        self.cmr_granules: Iterable[CMR_Granule] = cmr_search(config)
    
    def fetch(self):
        for cmr_granule in self.cmr_granules:
            filename = cmr_granule.url.split('/')[-1]
            # Get date from filename and convert to dt object
            date = file_utils.get_date(self.filename_date_regex, filename)
            dt = datetime.strptime(date, self.filename_date_fmt)
            if not (self.start_time_dt <= dt) and (self.end_time_dt >= dt):
                continue
            
            year = str(dt.year)

            local_fp = f'{self.target_dir}{year}/{filename}'

            if not os.path.exists(f'{self.target_dir}{year}/'):
                os.makedirs(f'{self.target_dir}{year}/')
                
            if harvesting_utils.check_update(self.solr_docs, filename, cmr_granule.mod_time):
                success = True
                granule = Granule(self.ds_name, local_fp, dt, cmr_granule.mod_time, cmr_granule.url)
                
                if self.need_to_download(granule):
                    logging.info(f'Downloading {filename} to {local_fp}')
                    try:
                        self.dl_file(cmr_granule.url, local_fp)
                    except:
                        success = False
                else:
                    logging.debug(f'{filename} already downloaded and up to date')
                    
                granule.update_item(self.solr_docs, success)
                granule.update_descendant(self.descendant_docs, success)
                self.updated_solr_docs.extend(granule.get_solr_docs())
        logging.info(f'Downloading {self.ds_name} complete')
            
    def get_mod_time(self, id: str) -> datetime:
        meta_url = f'https://cmr.earthdata.nasa.gov/search/concepts/{id}.json'
        r = requests.get(meta_url)
        meta = r.json()
        modified_time = datetime.strptime(f'{meta["updated"].split(".")[0]}Z', self.solr_format)
        return modified_time
    
    def dl_file(self, src: str, dst: str):
        r = requests.get(src)
        r.raise_for_status()
        open(dst, 'wb').write(r.content)


    def fetch_atl_daily(self):
        for cmr_granule in self.cmr_granules:
            filename = cmr_granule.url.split('/')[-1]
            # Get date from filename and convert to dt object
            date = file_utils.get_date(self.filename_date_regex, filename)
            dt = datetime.strptime(date, self.filename_date_fmt)
            if not (self.start_time_dt <= dt) and (self.end_time_dt >= dt):
                continue
            
            year = str(dt.year)
            month = str(dt.month)

            local_fp = f'{self.target_dir}{year}/{filename}'

            if not os.path.exists(f'{self.target_dir}{year}/'):
                os.makedirs(f'{self.target_dir}{year}/')
                
            if harvesting_utils.check_update(self.solr_docs, filename, cmr_granule.mod_time):
                native_granule = Granule(self.ds_name, local_fp, dt, cmr_granule.mod_time, cmr_granule.url)

                if self.need_to_download(native_granule):
                    logging.info(f'Downloading {filename} to {local_fp}')
                    try:
                        self.dl_file(cmr_granule.url, local_fp)
                        ds = xr.open_dataset(local_fp, decode_times=True)
                        ds = ds[['grid_x', 'grid_y', 'crs']]
                        
                        for i in range(1,32):
                            success = True
                            day_number = str(i).zfill(2)
                            try:
                                var_ds = xr.open_dataset(local_fp, group=f'daily/day{day_number}')
                            except:
                                continue
                            mid_date = (var_ds.delta_time_beg.values[0] + ((var_ds.delta_time_end.values[0] - var_ds.delta_time_beg.values[0]) / 2)).astype(str)[:10]
                            date = np.datetime64(mid_date)
                            dt = datetime(int(year), int(month), i)
                            time_var_ds = var_ds.expand_dims({'time': [date]})
                            time_var_ds = time_var_ds[[field['name'] for field in self.config['fields']]]
                            merged_ds = xr.merge([ds, time_var_ds])
                            
                            daily_filename = filename[:9] + year + month + day_number + filename[-10:-3] + '.nc'
                            daily_local_fp = f'{self.target_dir}{year}/{daily_filename}'
                            merged_ds.to_netcdf(daily_local_fp)
                            
                            daily_granule = Granule(self.ds_name, daily_local_fp, dt, cmr_granule.mod_time, cmr_granule.url)
                            daily_granule.update_item(self.solr_docs, success)
                            daily_granule.update_descendant(self.descendant_docs, success)
                            self.updated_solr_docs.extend(daily_granule.get_solr_docs())
                    except Exception as e:
                        print(e)
                        success = False
                else:
                    logging.debug(f'{filename} already downloaded and up to date')
        logging.info(f'Downloading {self.ds_name} complete')


    def fetch_tellus_grac_grfo(self):
        for cmr_granule in self.cmr_granules:
            filename = cmr_granule.url.split('/')[-1]
            # Get date from filename and convert to dt object
            date = file_utils.get_date(self.filename_date_regex, filename)
            dt = datetime.strptime(date, self.filename_date_fmt)
            if not (self.start_time_dt <= dt) and (self.end_time_dt >= dt):
                continue
            
            year = str(dt.year)
            month = str(dt.month)

            local_fp = f'{self.target_dir}{year}/{filename}'

            if not os.path.exists(f'{self.target_dir}{year}/'):
                os.makedirs(f'{self.target_dir}{year}/')
                
            if harvesting_utils.check_update(self.solr_docs, filename, cmr_granule.mod_time):
                native_granule = Granule(self.ds_name, local_fp, dt, cmr_granule.mod_time, cmr_granule.url)

                if self.need_to_download(native_granule):
                    logging.info(f'Downloading {filename} to {local_fp}')
                    try:
                        self.dl_file(cmr_granule.url, local_fp)
                        ds = xr.open_dataset(local_fp, decode_times=True)
                        
                        for time in ds.time.values:
                            success = True
                            sub_ds = ds.sel(time=time)
                            time_dt = datetime.strptime(str(time)[:-3], "%Y-%m-%dT%H:%M:%S.%f")
                            filename_time = str(time_dt)[:10].replace('-', '')

                            slice_filename = f'{self.ds_name}_{filename_time}.nc'
                            slice_local_fp = f'{self.target_dir}{year}/{slice_filename}'
                            sub_ds.to_netcdf(slice_local_fp)
                            
                            monthly_granule = Granule(self.ds_name, slice_local_fp, dt, cmr_granule.mod_time, cmr_granule.url)
                            monthly_granule.update_item(self.solr_docs, success)
                            monthly_granule.update_descendant(self.descendant_docs, success)
                            self.updated_solr_docs.extend(monthly_granule.get_solr_docs())
                    except Exception as e:
                        print(e)
                        success = False
                else:
                    logging.debug(f'{filename} already downloaded and up to date')
        logging.info(f'Downloading {self.ds_name} complete')


    def fetch_rdeft4(self):
        
        # Data filenames contain end of time coverage. To get data covering an
        # entire calendar month, we only look at files with the last day of the
        # month in the filename.
        
        # Consider looking for closest date (within 2 or 3 days) to end of month if end of month is unavailable
        
        years = np.arange(int(self.start_time_dt.year), int(self.end_time_dt.year) + 1)
        end_of_month = [datetime(year, month, calendar.monthrange(year,month)[1]) for year in years for month in range(1,13)]
        url_dict = {granule.url.split('RDEFT4_')[-1].split('.')[0]: granule for granule in self.cmr_granules} # granule end date:url

        end_of_month_granules = []
        for month_end in end_of_month:
            month_end_str = month_end.strftime(self.config['filename_date_fmt'])
            if month_end_str in url_dict.keys():
                end_of_month_granules.append(url_dict[month_end_str])
            else:
                for tolerance_num in range(-1,-3,-1):
                    new_date = month_end + timedelta(days=tolerance_num)
                    month_end_str = new_date.strftime(self.config['filename_date_fmt'])
                    if month_end_str in url_dict.keys():
                        end_of_month_granules.append(url_dict[month_end_str])
                        break

        self.cmr_granules = end_of_month_granules

        for cmr_granule in self.cmr_granules:
            filename = cmr_granule.url.split('/')[-1]
            # Get date from filename and convert to dt object
            date = file_utils.get_date(self.filename_date_regex, filename)
            dt = datetime.strptime(date, self.filename_date_fmt)
            
            # Force date to be first of the month
            dt = dt.replace(day=1)
            date = dt.strftime(self.filename_date_regex)
            
            if not (self.start_time_dt <= dt) and (self.end_time_dt >= dt):
                continue
            
            year = str(dt.year)

            local_fp = f'{self.target_dir}{year}/{filename}'

            if not os.path.exists(f'{self.target_dir}{year}/'):
                os.makedirs(f'{self.target_dir}{year}/')
                
            if harvesting_utils.check_update(self.solr_docs, filename, cmr_granule.mod_time):
                success = True
                granule = Granule(self.ds_name, local_fp, dt, cmr_granule.mod_time, cmr_granule.url)
                
                if self.need_to_download(granule):
                    logging.info(f'Downloading {filename} to {local_fp}')
                    try:
                        self.dl_file(cmr_granule.url, local_fp)
                    except:
                        success = False
                else:
                    logging.debug(f'{filename} already downloaded and up to date')
                    
                granule.update_item(self.solr_docs, success)
                granule.update_descendant(self.descendant_docs, success)
                self.updated_solr_docs.extend(granule.get_solr_docs())
        logging.info(f'Downloading {self.ds_name} complete')
                


def harvester(config: dict) -> str:
    """
    Uses CMR search to find granules within date range given in harvester_config.yaml.
    Creates (or updates) Solr entries for dataset, harvested granule, and descendants.
    """

    harvester = CMR_Harvester(config)
    if harvester.ds_name == 'ATL20_V004_daily':
        harvester.fetch_atl_daily()
    elif harvester.ds_name == 'TELLUS_GRAC-GRFO_MASCON_CRI_GRID_RL06.1_V3':
        harvester.fetch_tellus_grac_grfo()
    elif harvester.ds_name == 'RDEFT4':
        harvester.fetch_rdeft4()
    else:
        harvester.fetch()
    harvesting_status = harvester.post_fetch()
    return harvesting_status