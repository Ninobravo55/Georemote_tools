from qgis.PyQt.QtWidgets import QAction, QMenu, QMessageBox
from qgis.PyQt.QtCore import QTranslator, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication, QgsSettings, QgsMessageLog, Qgis
import os

from .Script._qt_compat import qt_exec
from .geomaticape_provider import GeomaticapeProvider

# Importar algoritmos desde el provider para evitar duplicar imports en memoria
from .geomaticape_provider import (
    FirmaEspectral, RSLandSatC2L1, FactorLandsat, FactorSentinel2L1A, FactorSentinel2L2A,
    FactorMODIS09, FactorMODIS11, FactorMODIS12, FactorMODIS13, FactorMODIS43,
    CBERS04APansharp, LandsatPansharpening, ACPSatelite, IndicesEspectrales,
    IndicesEspectralesSeleccion,
    ClasificacionNoSupervisada, ClasificacionSupervisada, ExtraerBandasMultiespectral,
    CombinarBandasNombres, RecortarRastersZona, TasseledCap, ClasificarRaster,
    ReclasificarRaster, ReporteClasificacion, ReporteClasificacionVectorial,
    RasterMosaicoImagenes, RasterDefinirCeldasNulas, ReproyectarRaster,
    CrearPoligonosTabla, EstadisticaZonalRaster, ExtraerValoresPuntuales,
    VectorAnguloPoligono, VectorPoligonoSuperpuesto, VectorSucesionCruzada,
    MDEDescargarMDE, MDEPuntoCotaDEM, MDECurvasNivelIntermedias,
    GEEDescargarImagenes, GEEDescargarIndices, GEEFirmaEspectral
)

# GEE Auth Dialog (interfaz que no pertenece al provider)
from .Script.gee_auth_dialog import GEEAuthDialog


class GeomaticapePlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = None
        self.menu_conv = None
        self.menu_proc = None
        self.menu_geo = None
        self.menu_post = None
        self.menu_mde = None
        self.menu_gee = None
        self.provider = None
        user_locale = QgsSettings().value('locale/userLocale', 'en')
        self.locale = user_locale[0:2] if user_locale else 'en'
        self.translator = QTranslator()
        i18n_path = os.path.join(
            self.plugin_dir, 'i18n', f'geomaticape_{self.locale}.qm')
        if os.path.exists(i18n_path):
            self.translator.load(i18n_path)
            QCoreApplication.installTranslator(self.translator)

    def tr(self, message):
        return QCoreApplication.translate('GeomaticaPe', message)

    def initProcessing(self):
        self.provider = GeomaticapeProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):

        try:
            from osgeo import gdal
            major_version = int(gdal.__version__.split('.')[0])
            if major_version < 3:
                msg = QMessageBox()
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.setWindowTitle(
                    "Advertencia de Compatibilidad - GeoRemote Tools")
                msg.setText(
                    f"Versión de GDAL muy antigua ({gdal.__version__})")
                msg.setInformativeText(
                    "GeoRemote Tools requiere GDAL 3.0 o superior para procesar subdatasets HDF4 (MODIS) y generar rasteres comprimidos con BIGTIFF de manera segura.\n\nEs probable que algunas herramientas fallen. Te recomendamos actualizar a QGIS 3.28 o superior.")
                msg.setStandardButtons(QMessageBox.StandardButton.Ok)
                qt_exec(msg)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"No se pudo verificar la versión de GDAL: {e}",
                "Geomaticape", Qgis.Warning)

        logo_path = os.path.join(
            self.plugin_dir,
            "Icons",
            "logo_geomatica.png")
        self.menu = QMenu(self.tr("GeoRemote Tools"), self.iface.mainWindow())
        self.menu.setIcon(QIcon(logo_path))
        self.iface.pluginMenu().addMenu(self.menu)

        # Herramienta manual para instalar dependencias
        action_deps = QAction(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "Instal.png")),
            self.tr("Instalar dependencias de Python..."),
            self.iface.mainWindow())
        action_deps.triggered.connect(self.check_dependencies_manual)
        self.menu.addAction(action_deps)
        self.actions.append(action_deps)
        self.menu.addSeparator()

        # ── CONVERSION ──────────────────────────────────────────
        self.menu_conv = QMenu(self.tr("Conversion"), self.menu)
        self.menu_conv.setIcon(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "landsat.png")))
        self.menu.addMenu(self.menu_conv)

        self.menu_conv.addSeparator()

        # Landsat / Sentinel
        self.add_action(self.menu_conv, self.tr("Factor escala Landsat C2 L1"),
                        "Icons/landsat.png", RSLandSatC2L1)
        self.add_action(self.menu_conv, self.tr("Factor escala Landsat C2 L2"),
                        "Icons/landsat.png", FactorLandsat)
        self.add_action(self.menu_conv, self.tr("Factor escala Sentinel2 L1A"),
                        "Icons/sentinel2l1a.png", FactorSentinel2L1A)
        self.add_action(self.menu_conv, self.tr("Factor escala Sentinel2 L2A"),
                        "Icons/sentinel2l2a.png", FactorSentinel2L2A)

        self.menu_conv.addSeparator()

        # MODIS — 4 herramientas independientes
        self.add_action(self.menu_conv, self.tr("Factor escala MODIS 09 (Reflectancia Superficial)"),
                        "Icons/indices.png", FactorMODIS09)
        self.add_action(self.menu_conv, self.tr("Factor escala MODIS 11 (LST °C)"),
                        "Icons/indices.png", FactorMODIS11)
        self.add_action(self.menu_conv, self.tr("Factor escala MODIS 12 (Cobertura del Suelo)"),
                        "Icons/clasificacion.png", FactorMODIS12)
        self.add_action(self.menu_conv, self.tr("Factor escala MODIS 13 (NDVI / EVI)"),
                        "Icons/indices.png", FactorMODIS13)
        self.add_action(self.menu_conv, self.tr("Factor escala MODIS 43 (BRDF / Albedo / NBAR)"),
                        "Icons/indices.png", FactorMODIS43)

        self.menu.addSeparator()

        # ── PROCESAMIENTO ────────────────────────────────────────
        self.menu_proc = QMenu(self.tr("Procesamiento"), self.menu)
        self.menu_proc.setIcon(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "acp.png")))
        self.menu.addMenu(self.menu_proc)

        self.add_action(self.menu_proc, self.tr("CBERS-04A Pansharpening 2m"),
                        "Icons/CBERS04A.png", CBERS04APansharp)
        self.add_action(self.menu_proc, self.tr("Landsat Pansharpening 30m -> 15m (Weighted Brovey)"),
                        "Icons/landsat.png", LandsatPansharpening)
        self.add_action(self.menu_proc, self.tr("ACP Multiespectral (cualquier satelite)"),
                        "Icons/acp.png", ACPSatelite)
        self.add_action(self.menu_proc, self.tr("Tasseled Cap (Brightness · Greenness · Wetness)"),
                        "Icons/indices.png", TasseledCap)
        self.add_action(self.menu_proc, self.tr("Indices espectrales (NDVI, SAVI, EVI, NDWI...)"),
                        "Icons/indices.png", IndicesEspectrales)
        self.add_action(self.menu_proc, self.tr("Indices espectrales - Seleccion multiple (carpeta de salida)"),
                        "Icons/indices.png", IndicesEspectralesSeleccion)
        self.add_action(self.menu_proc, self.tr("Extraer bandas de imagenes multiespectrales"),
                        "Icons/extraer_bandas.png", ExtraerBandasMultiespectral)
        self.add_action(self.menu_proc, self.tr("Combinar bandas con nombres (Red, NIR, SWIR1...)"),
                        "Icons/combinar_bandas.png", CombinarBandasNombres)
        self.add_action(self.menu_proc, self.tr("Recortar raster por zona de estudio (cutline / bbox)"),
                        "Icons/poligonos_tabla.png", RecortarRastersZona)
        self.add_action(self.menu_proc, self.tr("Reproyectar raster"),
                        "Icons/indices.png", ReproyectarRaster)
        self.add_action(self.menu_proc, self.tr("Firma espectral (Landsat 5/7/8/9 · Sentinel-2 · ASTER)"),
                        "Icons/indices.png", FirmaEspectral)

        self.menu_proc.addSeparator()

        self.add_action(self.menu_proc, self.tr("Clasificacion no supervisada (K-Means, GMM, ISODATA, Birch)"),
                        "Icons/clasificacion.png", ClasificacionNoSupervisada)
        self.add_action(self.menu_proc, self.tr("Clasificacion supervisada y validacion"),
                        "Icons/clasif_supervisada.png", ClasificacionSupervisada)
        self.add_action(self.menu_proc, self.tr("Mosaico de imagenes"),
                        "Icons/indices.png", RasterMosaicoImagenes)
        self.add_action(self.menu_proc, self.tr("Definir celdas nulas"),
                        "Icons/indices.png", RasterDefinirCeldasNulas)

        self.menu.addSeparator()

        # ── POSTPROCESAMIENTO ────────────────────────────────────
        self.menu_post = QMenu(self.tr("PostProcesamiento"), self.menu)
        self.menu_post.setIcon(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "clasificacion.png")))
        self.menu.addMenu(self.menu_post)

        self.add_action(self.menu_post, self.tr("Clasificar raster por rangos (min / max / valor)"),
                        "Icons/clasificacion.png", ClasificarRaster)
        self.add_action(self.menu_post, self.tr("Reclasificar raster (remapeo de valores)"),
                        "Icons/clasificacion.png", ReclasificarRaster)
        self.add_action(self.menu_post, self.tr("Reporte de clasificacion (area · porcentaje · estadisticas)"),
                        "Icons/zonal_raster.png", ReporteClasificacion)
        self.add_action(self.menu_post, self.tr("Reporte clasificacion vectorial (area y graficos)"),
                        "Icons/zonal_raster.png", ReporteClasificacionVectorial)

        self.menu.addSeparator()

        # ── GEOPROCESAMIENTO ─────────────────────────────────────
        self.menu_geo = QMenu(self.tr("Geoprocesamiento"), self.menu)
        self.menu_geo.setIcon(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "poligonos_tabla.png")))
        self.menu.addMenu(self.menu_geo)

        self.add_action(self.menu_geo, self.tr("Crear poligonos a partir de tabla (CSV/XLSX/TXT)"),
                        "Icons/poligonos_tabla.png", CrearPoligonosTabla)
        self.add_action(self.menu_geo, self.tr("Estadistica zonal raster (Excel/CSV)"),
                        "Icons/zonal_raster.png", EstadisticaZonalRaster)
        self.add_action(self.menu_geo, self.tr("Extraer valores puntuales de multiples raster"),
                        "Icons/extraer_valores.png", ExtraerValoresPuntuales)

        self.add_action(self.menu_geo, self.tr("Calcular angulo de poligono"),
                        "Icons/poligonos_tabla.png", VectorAnguloPoligono)
        self.add_action(self.menu_geo, self.tr("Poligono superpuesto propio"),
                        "Icons/poligonos_tabla.png", VectorPoligonoSuperpuesto)
        self.add_action(self.menu_geo, self.tr("Secciones transversales"),
                        "Icons/poligonos_tabla.png", VectorSucesionCruzada)

        self.menu.addSeparator()

        # ── MDE ──────────────────────────────────────────────────
        self.menu_mde = QMenu(self.tr("MDE"), self.menu)
        self.menu_mde.setIcon(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "extraer_valores.png")))
        self.menu.addMenu(self.menu_mde)

        self.add_action(self.menu_mde, self.tr("Descargar MDE"),
                        "Icons/extraer_valores.png", MDEDescargarMDE)
        # self.add_action(self.menu_mde, self.tr("Establecer coordenada Z desde MDE"),
        #                "Icons/extraer_valores.png", MDEEstablecerZdesdeMDE)
        self.add_action(self.menu_mde, self.tr("Generar elevaciones puntuales"),
                        "Icons/extraer_valores.png", MDEPuntoCotaDEM)
        self.add_action(self.menu_mde, self.tr("Extraer curvas de nivel intermedias"),
                        "Icons/extraer_valores.png", MDECurvasNivelIntermedias)

        self.menu.addSeparator()

        # ── DESCARGA GEE ──────────────────────────────────────────
        self.menu_gee = QMenu(self.tr("Descarga GEE"), self.menu)
        self.menu_gee.setIcon(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "landsat.png")))
        self.menu.addMenu(self.menu_gee)

        # Autenticación global GEE
        action_auth = QAction(
            QIcon(
                os.path.join(
                    self.plugin_dir,
                    "Icons",
                    "landsat.png")),
            self.tr("Autenticar Google Earth Engine"),
            self.iface.mainWindow())
        action_auth.triggered.connect(self.run_gee_auth)
        self.menu_gee.addAction(action_auth)
        self.actions.append(action_auth)

        self.menu_gee.addSeparator()

        self.add_action(self.menu_gee, self.tr("Descargar imagenes Landsat / Sentinel-2"),
                        "Icons/landsat.png", GEEDescargarImagenes)
        self.add_action(self.menu_gee, self.tr("Descargar indices espectrales (Landsat / Sentinel-2)"),
                        "Icons/indices.png", GEEDescargarIndices)
        self.add_action(self.menu_gee, self.tr("Firma espectral profesional (GEE)"),
                        "Icons/indices.png", GEEFirmaEspectral)

    def add_action(self, parent_menu, text, icon_path, tool_class):
        icon = QIcon(os.path.join(self.plugin_dir, icon_path))
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(lambda: tool_class().run())
        parent_menu.addAction(action)
        self.actions.append(action)

    def run_gee_auth(self):
        dlg = GEEAuthDialog(self.iface.mainWindow())
        qt_exec(dlg)

    def unload(self):
        if self.menu:
            self.iface.pluginMenu().removeAction(self.menu.menuAction())
            self.menu = None
            self.menu_conv = None
            self.menu_proc = None
            self.menu_geo = None
            self.menu_post = None
            self.menu_mde = None
            self.menu_gee = None

        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        if hasattr(self, 'translator') and self.translator:
            QCoreApplication.removeTranslator(self.translator)
            self.translator = None

    def check_dependencies_manual(self):
        missing = []
        try:
            import sklearn  # noqa: F401
        except ImportError:
            missing.append("scikit-learn")

        try:
            import pandas  # noqa: F401
        except ImportError:
            missing.append("pandas")

        try:
            import openpyxl  # noqa: F401
        except ImportError:
            missing.append("openpyxl")

        try:
            import xgboost  # noqa: F401
        except ImportError:
            missing.append("xgboost")

        try:
            import catboost  # noqa: F401
        except ImportError:
            missing.append("catboost")

        try:
            import matplotlib  # noqa: F401
        except ImportError:
            missing.append("matplotlib")

        try:
            import ee  # noqa: F401
        except ImportError:
            missing.append("earthengine-api")

        if missing:
            msgBox = QMessageBox(self.iface.mainWindow())
            msgBox.setIcon(QMessageBox.Icon.Warning)
            msgBox.setWindowTitle("GeoRemote Tools - Instalar Dependencias")
            msgBox.setText(
                f"Faltan instalar las siguientes bibliotecas de Python:\n\n{', '.join(missing)}\n\n¿Desea instalarlas ahora? (Nota: QGIS se congelará temporalmente mientras descarga los archivos).")
            msgBox.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            msgBox.setDefaultButton(QMessageBox.StandardButton.Yes)

            # qt_exec() es compatible con PyQt5 (exec_) y PyQt6 (exec)
            if qt_exec(msgBox) == QMessageBox.StandardButton.Yes:
                self.install_dependencies(missing)
        else:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Dependencias Completas",
                "Todas las dependencias de Python requeridas ya están instaladas. No necesitas hacer nada más.")

    def install_dependencies(self, missing):
        from .Script.install_deps_dialog import InstallDepsDialog
        self.deps_dialog = InstallDepsDialog(missing, self.iface.mainWindow())
        self.deps_dialog.show()
