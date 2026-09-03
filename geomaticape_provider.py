import os
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

# Conversion
from .Script.firma_espectral import FirmaEspectral
from .Script.rs_landsat_c2_l1 import RSLandSatC2L1
from .Script.factor_landsat import FactorLandsat
from .Script.factor_sentinel2_l1a import FactorSentinel2L1A
from .Script.factor_sentinel2_l2a import FactorSentinel2L2A
from .Script.factor_modis09 import FactorMODIS09
from .Script.factor_modis11 import FactorMODIS11
from .Script.factor_modis12 import FactorMODIS12
from .Script.factor_modis13 import FactorMODIS13
from .Script.factor_modis43 import FactorMODIS43

# Procesamiento
from .Script.cbers04a_pansharp import CBERS04APansharp
from .Script.landsat_pansharpening import LandsatPansharpening
from .Script.acp_satelite import ACPSatelite
from .Script.indices_espectrales import IndicesEspectrales
from .Script.indices_espectrales_seleccion import IndicesEspectralesSeleccion
from .Script.clasificacion_no_supervisada import ClasificacionNoSupervisada
from .Script.clasificacion_supervisada import ClasificacionSupervisada
from .Script.extraer_bandas_multiespectral import ExtraerBandasMultiespectral
from .Script.combinar_bandas_nombres import CombinarBandasNombres
from .Script.recortar_rasters_zona import RecortarRastersZona
from .Script.tasseled_cap import TasseledCap
from .Script.clasificar_raster import ClasificarRaster
from .Script.reclasificar_raster import ReclasificarRaster
from .Script.reporte_clasificacion import ReporteClasificacion
from .Script.reporte_clasificacion_vectorial import ReporteClasificacionVectorial
from .Script.raster_mosaico_imagenes import RasterMosaicoImagenes
from .Script.raster_definir_celdas_nulas import RasterDefinirCeldasNulas
from .Script.reproyectar_raster import ReproyectarRaster

# Geoprocesamiento
from .Script.crear_poligonos_tabla import CrearPoligonosTabla
from .Script.estadistica_zonal_raster import EstadisticaZonalRaster
from .Script.extraer_valores_puntuales import ExtraerValoresPuntuales
from .Script.vector_angulo_poligono import VectorAnguloPoligono
from .Script.vector_poligono_superpuesto import VectorPoligonoSuperpuesto
from .Script.vector_sucesion_cruzada import VectorSucesionCruzada

# MDE
from .Script.mde_descargar_mde import MDEDescargarMDE
from .Script.mde_punto_cota_dem import MDEPuntoCotaDEM
from .Script.mde_curvas_nivel_intermedias import MDECurvasNivelIntermedias

# GEE
from .Script.gee_descargar_imagenes import GEEDescargarImagenes
from .Script.gee_descargar_indices import GEEDescargarIndices
from .Script.gee_firma_espectral import GEEFirmaEspectral


class GeomaticapeProvider(QgsProcessingProvider):

    def __init__(self):
        QgsProcessingProvider.__init__(self)

    def unload(self):
        pass

    def loadAlgorithms(self):
        # Conversion
        self.addAlgorithm(FirmaEspectral())
        self.addAlgorithm(RSLandSatC2L1())
        self.addAlgorithm(FactorLandsat())
        self.addAlgorithm(FactorSentinel2L1A())
        self.addAlgorithm(FactorSentinel2L2A())
        self.addAlgorithm(FactorMODIS09())
        self.addAlgorithm(FactorMODIS11())
        self.addAlgorithm(FactorMODIS12())
        self.addAlgorithm(FactorMODIS13())
        self.addAlgorithm(FactorMODIS43())

        # Procesamiento
        self.addAlgorithm(CBERS04APansharp())
        self.addAlgorithm(LandsatPansharpening())
        self.addAlgorithm(ACPSatelite())
        self.addAlgorithm(IndicesEspectrales())
        self.addAlgorithm(IndicesEspectralesSeleccion())
        self.addAlgorithm(ClasificacionNoSupervisada())
        self.addAlgorithm(ClasificacionSupervisada())
        self.addAlgorithm(ExtraerBandasMultiespectral())
        self.addAlgorithm(CombinarBandasNombres())
        self.addAlgorithm(RecortarRastersZona())
        self.addAlgorithm(TasseledCap())
        self.addAlgorithm(ClasificarRaster())
        self.addAlgorithm(ReclasificarRaster())
        self.addAlgorithm(ReporteClasificacion())
        self.addAlgorithm(ReporteClasificacionVectorial())
        self.addAlgorithm(RasterMosaicoImagenes())
        self.addAlgorithm(RasterDefinirCeldasNulas())
        self.addAlgorithm(ReproyectarRaster())

        # Geoprocesamiento
        self.addAlgorithm(CrearPoligonosTabla())
        self.addAlgorithm(EstadisticaZonalRaster())
        self.addAlgorithm(ExtraerValoresPuntuales())
        self.addAlgorithm(VectorAnguloPoligono())
        self.addAlgorithm(VectorPoligonoSuperpuesto())
        self.addAlgorithm(VectorSucesionCruzada())

        # MDE
        self.addAlgorithm(MDEDescargarMDE())
        self.addAlgorithm(MDEPuntoCotaDEM())
        self.addAlgorithm(MDECurvasNivelIntermedias())

        # GEE
        self.addAlgorithm(GEEDescargarImagenes())
        self.addAlgorithm(GEEDescargarIndices())
        self.addAlgorithm(GEEFirmaEspectral())

    def id(self):
        return 'geomaticape'

    def name(self):
        return 'GeoRemote Tools'

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(
            __file__), 'Icons', 'logo_geomatica.png'))

    def longName(self):
        return self.name()
