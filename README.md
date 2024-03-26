# ECCO-PIPELINE

## Summary
The ECCO_OBS_PIPELINE exists as a framework to regularly harvest and transform datasets used as inputs for the ECCO model. 

Data is harvested from multiple sources (PO.DAAC, NSIDC, OSISAF, iFremer) and transformed to a variety of grids and formats (binary and netCDF, daily and monthly average). A Solr database is utilized for maintaining the state of a dataset between pipeline runs.

## Documentation
Documentation is in the process of being overhauled. Legacy documentation can be found on the repo's wiki: https://github.com/ECCO-GROUP/ECCO-obs-pipeline/wiki. 

## Setup

### Requirements
- Solr
- Conda
- .netrc file containing Earthdata login credentials

### Start Solr and Setup core
```
cd <path/to/solr/directory>
bin/solr start
bin/solr create -c ecco_datasets
```

### Clone repo
```
git clone https://github.com/ECCO-GROUP/ECCO-obs-pipeline.git
```

### Install dependencies via Conda envrionment 
```
cd <path/to/cloned/repo>
conda env create -f environment.yml
conda activate ecco_pipeline
```

### Setup global_settings.py
```
cp ecco_pipeline/conf/global_settings.py.example ecco_pipeline/conf/global_settings.py
```
Fill in variables.

### Running pipeline
```
python ecco_pipeline/run_pipeline.py
```

The default logging level is set to `info` but is adjustable via the `--log_level` flag when running the pipeline. ex:
```
python ecco_pipeline/run_pipeline.py --log_level DEBUG
```
