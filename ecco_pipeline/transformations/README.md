# Transformations

## Transformation job factory
Determines which target grid/field combinations a single granule for a single dataset need to be transformed, either because the transformation has not yet happened, the harvested granule has been updated, or the transformation configuration has been modified.

Mapping factors are generated and locally cached if needed, and preloaded along with the grids in objects referred to by transformation code, reducing I/O.

Supports Python's multiprocessing to execute transformations in parallel.

## Transformation

A grid transformation occurs for a single data granule to a single target grid for a single field. 

1. Make mapping factors (ie: mappings from source to target grid) via `utils.processing_utils.transformation_utils.generalized_grid_product()` -> `utils.processing_utils.transformation_utils.find_mappings_from_source_to_target()`. These are cached on disk and get reused for future pipeline runs for a given dataset. 

2. An arbitrary number of preprocessing functions can be applied to the data prior to transformation to the target grid. ex: masking flagged data

2. Make array of target shape with transformed (or reprojected) data values via `utils.processing_utils.transformation_utils.transform_to_target_grid()`

3. Apply arbitrary number of postprocessing functions to the data. ex: converting units

4. Metadata is set, the transformed netCDF is saved, and the Solr database is updated

If an error occurs during the transformation process, an "empty record" is saved instead. 