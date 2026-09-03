from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
Reproyectar raster
Geomaticape Plugin - Procesamiento
Autor: GEOMATICA AMBIENTAL
"""

from qgis.core import (
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterCrs,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException
)
from qgis import processing
from osgeo import gdal


class ReproyectarRaster(GeomaticapeAlgorithm):
    _algorithm_name = "reproyectar_raster"
    _icon_name = "indices.png"
    INPUT = 'INPUT'
    TARGET_CRS = 'TARGET_CRS'
    OUTPUT = 'OUTPUT'

    def displayName(self):
        return self.tr('Reproyectar raster')

    def group(self):
        return self.tr('Procesamiento')

    def groupId(self):
        return 'geomaticape_procesamiento'

    def shortHelpString(self):
        return """
<h3>Reproyectar raster</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br><br>
<b>Descripción:</b><br>
Reproyecta un raster (monobanda o multibanda) a un nuevo Sistema de Referencia de Coordenadas (CRS)
definido por el usuario, <b>manteniendo intactos los nombres originales de las bandas</b>.
<br><br>
Muy útil para unificar proyecciones antes de realizar análisis multicriterio o entrenamientos de clasificación
sin perder la descripción de cada banda en el archivo resultante.
<br><br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT,
                self.tr('Raster de entrada (monobanda o multibanda)')
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.TARGET_CRS,
                self.tr('Sistema de Referencia de Coordenadas (CRS) de destino'),
                'EPSG:4326'
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                self.tr('Raster reproyectado')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        input_layer = self.parameterAsRasterLayer(
            parameters, self.INPUT, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
        output_path = self.parameterAsOutputLayer(
            parameters, self.OUTPUT, context)

        if input_layer is None:
            raise QgsProcessingException('Capa de entrada no válida.')

        input_path = input_layer.source()
        target_crs_authid = target_crs.authid()

        feedback.pushInfo(
            "====================================================")
        feedback.pushInfo(
            "              REPROYECTAR RASTER                    ")
        feedback.pushInfo(
            "====================================================")
        feedback.pushInfo(f"Archivo entrada : {input_path}")
        feedback.pushInfo(f"CRS Destino     : {target_crs_authid}")

        ds = gdal.Open(input_path, gdal.GA_ReadOnly)
        if ds is None:
            raise QgsProcessingException(
                'No se pudo abrir el raster de entrada con GDAL.')

        # Leer informacion de las bandas
        num_bands = ds.RasterCount
        band_names = []
        nodata_values = []

        feedback.pushInfo(f"Bandas detectadas: {num_bands}")
        for i in range(1, num_bands + 1):
            if 'feedback' in locals() and feedback.isCanceled():
                break
            b = ds.GetRasterBand(i)
            name = b.GetDescription()
            nodata = b.GetNoDataValue()
            band_names.append(name)
            nodata_values.append(nodata)
            feedback.pushInfo(
                f"  Banda {i}: {
                    name if name else '(sin nombre)'}")

        feedback.setProgress(10)

        # Usar gdal.Warp para reproyectar
        feedback.pushInfo(
            "\nReproyectando raster... esto puede tardar según el tamaño.")
        warp_options = gdal.WarpOptions(
            dstSRS=target_crs_authid,
            format='GTiff',
            creationOptions=['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER'],
            resampleAlg=gdal.GRA_NearestNeighbour
        )

        out_ds = gdal.Warp(output_path, ds, options=warp_options)

        if out_ds is None:
            raise QgsProcessingException(
                'Fallo al reproyectar el raster mediante GDAL.')

        feedback.setProgress(80)

        # Restaurar los nombres de las bandas y los valores nodata
        feedback.pushInfo("Restaurando nombres de las bandas...")
        for i in range(1, num_bands + 1):
            if 'feedback' in locals() and feedback.isCanceled():
                break
            out_b = out_ds.GetRasterBand(i)
            if band_names[i - 1]:
                out_b.SetDescription(band_names[i - 1])
            if nodata_values[i - 1] is not None:
                out_b.SetNoDataValue(nodata_values[i - 1])

        out_ds.FlushCache()
        out_ds = None
        ds = None

        feedback.setProgress(100)
        feedback.pushInfo(
            "\n====================================================")
        feedback.pushInfo(f"Raster guardado con éxito en:")
        feedback.pushInfo(f"{output_path}")

        return {self.OUTPUT: output_path}

    def run(self):
        processing.execAlgorithmDialog(self)
