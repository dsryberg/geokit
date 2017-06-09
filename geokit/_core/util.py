import os, sys, re
import numpy as np
import gdal, ogr, osr
from tempfile import TemporaryDirectory, NamedTemporaryFile
from glob import glob
import warnings
from collections import namedtuple, Iterable
import pandas as pd
from scipy.stats import describe
from scipy.interpolate import RectBivariateSpline

######################################################################################
# test modules

# The main SRS for lat/lon coordinates
_test = osr.SpatialReference()
res = _test.ImportFromEPSG(4326)

# Quick check if gdal loaded properly
if(not res==0 ):
    raise RuntimeError("GDAL did not load properly. Check your 'GDAL_DATA' environment variable")

######################################################################################
# An few errors just for me!
class GeoKitError(Exception): pass
class GeoKitSRSError(GeoKitError): pass
class GeoKitGeomError(GeoKitError): pass
class GeoKitRasterError(GeoKitError): pass
class GeoKitVectorError(GeoKitError): pass
class GeoKitExtentError(GeoKitError): pass
class GeoKitRegionMaskError(GeoKitError): pass

##################################################################
# General funcs
def isclose(a, b, rel_tol=1e-09, abs_tol=0.0):
    """***GIS INTERNAL***
    Convenience function for checking if two float vlaues a 'close-enough'
    """
    return abs(a-b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)

# matrix scaler
def scaleMatrix(mat, scale, strict=True):
    """Scale a 2-dimensional matrix. For example, a 2x2 matrix, with a scale of 2, will become a 4x4 matrix. Or
    scaling a 24x24 matrix with a scale of -3 will produce an 8x8 matrix.

    * Scaling UP (positive) results in a dimensionally larger matrix where each value is repeated scale^2 times
    * scaling DOWN (negative) results in a dimensionally smaller matrix where each value is the average of the 
        associated 'up-scaled' block

    Inputs:
        mat - numpy.ndarray : A two-dimensional numpy nd array
            - [[numeric,],] : A Two dimensional matrix of numerical values
        
        scale - int : A dimensional scaling factor for both the x and y dimension
              - (int, int) : y-scaling demnsion, x-scaling dimension
              
              * If scaling down, the scaling factors must be a factor of the their associated dimension 
                in the input matrix (unless 'strict' is set fo False)
        
        strict - bool : Flags whether or not to force a fail when scaling-down by a scaling factor which is not a
                        dimensional factor
               * When scaling down by a non-dimensional factor, the matrix will be padded with zeros such that the new 
                 matrix has dimensional sizes which are divisible by the scaling factor. The points which are not at 
                 the right or bottom boundary are averaged, same as before. The points which lie on the edge however, 
                 are also averaged across all the values which lie in those pixels, but they are corrected so that the 
                 averaging does NOT take into account the padded zeros.

    EXAMPLES:

    INPUT       Scaleing Factor      Output
    -----       ---------------      ------

    | 1 2 |             2           | 1 1 2 2 |
    | 3 4 |                         | 1 1 2 2 |
                                    | 3 3 4 4 |
                                    | 3 3 4 4 |


    | 1 1 1 1 |        -2           | 1.5  2.0 | 
    | 2 2 3 3 |                     | 5.25 6.75|
    | 4 4 5 5 |
    | 6 7 8 9 |


    | 1 1 1 1 |        -3           | 2.55  3.0 |
    | 2 2 3 3 |   * strict=False    | 7.0    9  |
    | 4 4 5 5 |                       
    | 6 7 8 9 |       *padded*          
                    -------------
                   | 1 1 1 1 0 0 |
                   | 2 2 3 3 0 0 |
                   | 4 4 5 5 0 0 |
                   | 6 7 8 9 0 0 |
                   | 0 0 0 0 0 0 |
                   | 0 0 0 0 0 0 |

    """

    # unpack scale
    try:
        yScale,xScale = scale
    except:
        yScale,xScale = scale, scale

    # check for ints
    if( not (isinstance(xScale,int) and isinstance(yScale,int))):
        raise ValueError("scale must be integer types")

    if (xScale==0 and yScale==0): return mat # no scaling (it would just be silly to call this)
    elif (xScale>0 and yScale>0): # scale up
        out = np.zeros((mat.shape[0]*yScale, mat.shape[1]*xScale), dtype=mat.dtype)
        for yo in range(yScale):
            for xo in range(xScale):
                out[yo::yScale, xo::xScale] = mat
    
    elif (xScale<0 and yScale<0): # scale down
        xScale = -1*xScale
        yScale = -1*yScale
        # ensure scale is a factor of both xSize and ySize
        if strict:
            if( not( mat.shape[0]%yScale==0 and mat.shape[1]%xScale==0)):
                raise GeoKitError("Matrix can only be scaled down by a factor of it's dimensions")
            yPad = 0
            xPad = 0
        else:
            yPad = yScale-mat.shape[0]%yScale # get the amount to pad in the y direction
            xPad = xScale-mat.shape[1]%xScale # get the amount to pad in the x direction
            
            if yPad==yScale: yPad=0
            if xPad==xScale: xPad=0

            # Do y-padding
            if yPad>0: mat = np.concatenate( (mat, np.zeros((yPad,mat.shape[1])) ), 0)
            if xPad>0: mat = np.concatenate( (mat, np.zeros((mat.shape[0],xPad)) ), 1)
        
        out = np.zeros((mat.shape[0]//yScale, mat.shape[1]//xScale), dtype="float")
        for yo in range(yScale):
            for xo in range(xScale):
                out += mat[yo::yScale, xo::xScale]
        out = out/(xScale*yScale)

        # Correct the edges if a padding was provided
        if yPad>0: out[:-1,-1] *= yScale/(yScale-yPad) # fix the right edge EXCLUDING the bot-left point
        if xPad>0: out[-1,:-1] *= xScale/(xScale-xPad) # fix the bottom edge EXCLUDING the bot-left point
        if yPad>0: out[-1,-1]  *= yScale*xScale/(yScale-yPad)/(xScale-xPad) # fix the bot-left point

    else: # we have both a scaleup and a scale down
        raise GeoKitError("Dimensions must be scaled in the same direction")

    return out


def quickVector(geom, output=None):
    """GeoKit internal for quickly creating a vector datasource"""
    ######## Create a quick vector source
    if output:
        driver = gdal.GetDriverByName("ESRI Shapefile")
        dataSource = driver.Create( output, 0,0 )
    else:
        driver = gdal.GetDriverByName("Memory")
        
        # Using 'Create' from a Memory driver leads to an error. But creating
        #  a temporary shapefile driver (it doesnt actually produce a file, I think)
        #  and then using 'CreateCopy' seems to work
        tmp_driver = gdal.GetDriverByName("ESRI Shapefile")
        t = TemporaryDirectory()
        tmp_dataSource = tmp_driver.Create( t.name+"tmp.shp", 0, 0 )

        dataSource = driver.CreateCopy("MEMORY", tmp_dataSource)
        t.cleanup()
        del tmp_dataSource, tmp_driver, t

    # Create the layer and write feature
    layer = dataSource.CreateLayer( "", geom.GetSpatialReference(), geom.GetGeometryType() )
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry( geom )

    # Create the feature
    layer.CreateFeature( feature )
    feature.Destroy()

    # Done!
    if output: return output
    else: return dataSource


def quickRaster(bounds, srs, dx, dy, dType="GDT_Byte", noData=None, fill=None):
    """GeoKit internal for quickly creating a raster datasource"""
    xMin, yMin, xMax, yMax = bounds
    
    # Make a raster dataset and pull the band/maskBand objects
    cols = int(round((xMax-xMin)/pixelWidth)) # used 'round' instead of 'int' because this matched GDAL behavior better
    rows = int(round((yMax-yMin)/abs(pixelHeight)))
    originX = xMin
    originY = yMax # Always use the "Y-at-Top" orientation
    
    # Open the driver
    driver = gdal.GetDriverByName('Mem') # create a raster in memory
    raster = driver.Create('', cols, rows, 1, getattr(gdal,dtype))

    if(raster is None):
        raise GeoKitError("Failed to create temporary raster")

    raster.SetGeoTransform((originX, abs(pixelWidth), 0, originY, 0, -1*abs(pixelHeight)))
    
    # Set the SRS
    if not srs is None:
        rasterSRS = loadSRS(srs)
        raster.SetProjection( rasterSRS.ExportToWkt() )
    
    # get the band
    band = raster.GetRasterBand(1)

    # set nodata
    if noData: band.SetNoDataValue(noData)

    # do fill
    if fill: band.Fill(fillValue)

    # Done!
    band.FlushCache()
    del band
    raster.FlushCache()
    return raster