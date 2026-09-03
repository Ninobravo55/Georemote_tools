# -*- coding: utf-8 -*-
"""
Recortar raster por zona de estudio
=====================================
Recorta un raster usando una mascara/poligono definida por:
  * una capa vectorial (Shapefile, GeoPackage, GeoJSON, etc.), o
  * un raster de mascara binario (valores != nodata = area a conservar).

Funcionalidades:
  - Seleccion sencilla del raster a recortar.
  - Mascara / poligono: vectorial o raster.
  - Uso del poligono segun forma geometrica (cropToCutline) — activable.
  - Buffer opcional (metros) sobre el vector/mascara antes de recortar.
    Si hay varios poligonos, se disuelven en uno antes de aplicar el buffer.
  - Valor NoData: elimina del raster de salida los pixeles con ese valor.
  - Raster de salida recortada.

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.10
Grupo : Procesamiento
"""

from ._qt_compat import qt_exec
from qgis.core import QgsProcessingException
from .geomaticape_algorithm import GeomaticapeAlgorithm
import os
import tempfile

import numpy as np

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QMessageBox, QCheckBox, QDialogButtonBox,
    QProgressDialog, QApplication, QWidget, QDoubleSpinBox, QLineEdit
)
from qgis.core import (
    QgsProject, QgsRasterLayer, QgsMapLayerProxyModel,
    QgsMessageLog, Qgis
)
from qgis.gui import QgsMapLayerComboBox, QgsFileWidget

from osgeo import gdal, ogr, osr

from .combinar_bandas_nombres import (
    _DialogFeedback,
)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

COMPRESS_OPTIONS = ["LZW", "DEFLATE", "NONE", "ZSTD", "PACKBITS"]
RESAMPLE_OPTIONS = ["nearest", "bilinear", "cubic", "cubicspline",
                    "lanczos", "average", "mode"]


# ---------------------------------------------------------------------------
# Backend — procesamiento
# ---------------------------------------------------------------------------

def _dissolve_and_buffer_vector(vector_path, layer_name, buffer_m, feedback):
    """
    Disuelve todos los poligonos en uno (union) y aplica un buffer de
    buffer_m metros (en el SRS nativo del vector).
    Devuelve la ruta de un GeoPackage temporal.
    """
    tmp_path = os.path.join(
        tempfile.gettempdir(),
        f"_geomaticape_cutline_{os.getpid()}.gpkg"
    )

    src_ds = ogr.Open(vector_path)
    if src_ds is None:
        raise RuntimeError(f"OGR no pudo abrir: {vector_path}")

    src_lyr = (
        src_ds.GetLayerByName(layer_name)
        if layer_name else src_ds.GetLayer(0)
    )
    if src_lyr is None:
        src_ds = None
        raise RuntimeError("No se pudo leer la capa vectorial.")

    sref = src_lyr.GetSpatialRef()

    # Union de todos los poligonos en uno
    union_geom = None
    for feat in src_lyr:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        if union_geom is None:
            union_geom = geom.Clone()
        else:
            union_geom = union_geom.Union(geom)
    src_ds = None

    if union_geom is None:
        raise RuntimeError(
            "No se encontraron geometrias validas en el vector.")

    # Buffer (si > 0)
    if buffer_m and abs(buffer_m) > 1e-10:
        union_geom = union_geom.Buffer(buffer_m)
        if union_geom is None:
            raise RuntimeError(
                "El buffer fallo (posible problema de CRS/unidades).")

    # Escribir a GPKG temporal
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    drv = ogr.GetDriverByName("GPKG")
    dst_ds = drv.CreateDataSource(tmp_path)
    dst_lyr = dst_ds.CreateLayer("zona", srs=sref, geom_type=ogr.wkbPolygon)
    feat_def = dst_lyr.GetLayerDefn()
    out_feat = ogr.Feature(feat_def)
    out_feat.SetGeometry(union_geom)
    dst_lyr.CreateFeature(out_feat)
    out_feat = None
    dst_ds = None

    feedback.pushInfo(
        f"Cutline preparado: dissolve + buffer={buffer_m} m -> {tmp_path}"
    )
    return tmp_path, "zona"


def _raster_mask_to_vector(mask_path, buffer_m, feedback):
    """
    Convierte un raster de mascara (pixeles validos != nodata/0)
    en un poligono (via gdal.Polygonize) disuelto + buffer.
    Devuelve (vector_path, layer_name).
    """
    feedback.pushInfo("Convirtiendo raster de mascara a vector...")

    src_ds = gdal.Open(mask_path, gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(
            f"GDAL no pudo abrir la mascara raster: {mask_path}")

    src_band = src_ds.GetRasterBand(1)
    nodata_val = src_band.GetNoDataValue()

    # Crear mascara binaria en memoria
    data = src_band.ReadAsArray().astype(np.float64)
    if nodata_val is not None:
        valid_mask = (data != nodata_val) & np.isfinite(data)
    else:
        valid_mask = (data != 0) & np.isfinite(data)

    # Crear raster binario temporal para gdal.Polygonize
    mem_drv = gdal.GetDriverByName("MEM")
    mem_ds = mem_drv.Create(
        "", src_ds.RasterXSize, src_ds.RasterYSize, 1, gdal.GDT_Byte
    )
    mem_ds.SetGeoTransform(src_ds.GetGeoTransform())
    mem_ds.SetProjection(src_ds.GetProjection())
    mem_band = mem_ds.GetRasterBand(1)
    mem_band.WriteArray(valid_mask.astype(np.uint8))
    mem_band.SetNoDataValue(0)

    sref = osr.SpatialReference()
    sref.ImportFromWkt(src_ds.GetProjection())
    src_ds = None

    # Polygonize -> GPKG temporal
    tmp_path = os.path.join(
        tempfile.gettempdir(),
        f"_geomaticape_mask_poly_{os.getpid()}.gpkg"
    )
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    drv = ogr.GetDriverByName("GPKG")
    poly_ds = drv.CreateDataSource(tmp_path)
    poly_lyr = poly_ds.CreateLayer("mascara", srs=sref,
                                   geom_type=ogr.wkbPolygon)
    fd = ogr.FieldDefn("val", ogr.OFTInteger)
    poly_lyr.CreateField(fd)
    gdal.Polygonize(mem_band, mem_band, poly_lyr, 0, [], callback=None)
    poly_ds = None
    mem_ds = None

    feedback.pushInfo(f"Polygonize completado: {tmp_path}")

    # Disuelve + buffer usando la funcion vectorial
    return _dissolve_and_buffer_vector(tmp_path, "mascara", buffer_m, feedback)


def _prepare_cutline(mask_path, mask_is_raster, mask_layer_name,
                     buffer_m, feedback):
    """
    Retorna (cutline_path, layer_name, is_tmp) listo para gdal.Warp.
    is_tmp=True significa que hay que borrar el archivo al terminar.
    """
    if mask_is_raster:
        cut_path, cut_layer = _raster_mask_to_vector(
            mask_path, buffer_m, feedback)
        return cut_path, cut_layer, True
    else:
        if buffer_m and abs(buffer_m) > 1e-10:
            cut_path, cut_layer = _dissolve_and_buffer_vector(
                mask_path, mask_layer_name, buffer_m, feedback
            )
            return cut_path, cut_layer, True
        else:
            # Sin buffer: disolver de todas formas para unificar poligonos
            cut_path, cut_layer = _dissolve_and_buffer_vector(
                mask_path, mask_layer_name, 0.0, feedback
            )
            return cut_path, cut_layer, True


def ejecutar_recorte_simple(raster_path, mask_path, mask_is_raster,
                            mask_layer_name, crop_to_cutline,
                            buffer_m, nodata_value,
                            out_path, compress, feedback):
    """
    Recorta raster_path usando mask_path (vector o raster de mascara).
    Aplica buffer + dissolve al vector.
    Preserva los nombres de banda del raster de entrada.
    """
    if not raster_path:
        raise RuntimeError("Selecciona el raster a recortar.")
    if not mask_path:
        raise RuntimeError("Selecciona la mascara o poligono.")
    if not out_path:
        raise RuntimeError("Define la ruta del raster de salida.")

    feedback.pushInfo("=" * 64)
    feedback.pushInfo("Recortar raster por zona de estudio")
    feedback.pushInfo(f"Raster   : {os.path.basename(raster_path)}")
    feedback.pushInfo(f"Mascara  : {os.path.basename(mask_path)}"
                      f"  ({'raster' if mask_is_raster else 'vector'})")
    feedback.pushInfo(f"Buffer   : {buffer_m} m")
    feedback.pushInfo(f"CropGeom : {crop_to_cutline}")
    if nodata_value is not None:
        feedback.pushInfo(f"NoData   : {nodata_value}")
    feedback.pushInfo(f"Salida   : {out_path}")
    feedback.pushInfo("=" * 64)
    feedback.setProgress(5)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    # Leer nombres de bandas del raster fuente
    src_ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(f"GDAL no pudo abrir el raster: {raster_path}")
    nb = src_ds.RasterCount
    band_names = []
    for i in range(1, nb + 1):
        bd = src_ds.GetRasterBand(i)
        nm = (bd.GetDescription() or "").strip()
        if not nm:
            nm = f"band_{i}"
        band_names.append(nm)
    src_ds = None
    feedback.pushInfo(
        f"Bandas del raster fuente: {nb}  [{
            ', '.join(band_names)}]")
    feedback.setProgress(10)

    # Preparar cutline
    cut_path, cut_layer, is_tmp = _prepare_cutline(
        mask_path, mask_is_raster, mask_layer_name, buffer_m, feedback
    )
    feedback.setProgress(30)

    # Opciones de creacion
    creation = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if compress and compress != "NONE":
        creation.append(f"COMPRESS={compress}")

    warp_kw = dict(
        format="GTiff",
        multithread=True,
        creationOptions=creation,
    )

    if crop_to_cutline:
        warp_kw["cutlineDSName"] = cut_path
        warp_kw["cropToCutline"] = True
        if cut_layer:
            warp_kw["cutlineLayer"] = cut_layer
    else:
        # Recortar solo a la extension (bounding box) del poligono/mascara
        ds_vec = ogr.Open(cut_path)
        if ds_vec:
            lyr = ds_vec.GetLayerByName(
                cut_layer) if cut_layer else ds_vec.GetLayer(0)
            if lyr:
                ext = lyr.GetExtent()  # (minX, maxX, minY, maxY)
                warp_kw["outputBounds"] = (ext[0], ext[2], ext[1], ext[3])
            ds_vec = None
    if nodata_value is not None:
        warp_kw["dstNodata"] = float(nodata_value)

    feedback.pushInfo("Aplicando gdal.Warp...")
    try:
        result = gdal.Warp(out_path, raster_path, **warp_kw)
    except Exception as e:
        raise RuntimeError(f"gdal.Warp fallo: {e}")
    finally:
        if is_tmp:
            try:
                os.remove(cut_path)
            except OSError:
                pass

    if result is None:
        raise RuntimeError("gdal.Warp devolvio None. Revisa los parametros.")
    result = None
    feedback.setProgress(85)

    # Escribir nombres de banda en la salida
    ds_out = gdal.Open(out_path, gdal.GA_Update)
    if ds_out is not None:
        for i, nm in enumerate(band_names[:ds_out.RasterCount], start=1):
            b = ds_out.GetRasterBand(i)
            b.SetDescription(nm)
            try:
                b.SetMetadataItem("BAND_NAME", nm)
            except Exception as e:
                feedback.pushInfo(
                    f"Aviso: no se pudo etiquetar la banda {i} ('{nm}'): {e}")
        try:
            ds_out.SetMetadataItem(
                "GEOMATICAPE_BAND_ORDER", ",".join(band_names)
            )
        except Exception as e:
            feedback.pushInfo(
                f"Aviso: no se pudo escribir el orden de bandas en "
                f"metadatos: {e}")
        ds_out.FlushCache()
        ds_out = None
    feedback.setProgress(98)

    feedback.pushInfo("=" * 64)
    feedback.pushInfo(f"OK - Raster recortado: {out_path}")
    feedback.pushInfo("=" * 64)
    feedback.setProgress(100)
    return out_path


# ---------------------------------------------------------------------------
# Dialogo principal — interfaz simplificada nativa QGIS
# ---------------------------------------------------------------------------

class RecortarRastersZonaDialog(QDialog):
    """
    Interfaz simplificada de recorte raster, estilo nativo QGIS:
      1. Raster a Recortar (QgsMapLayerComboBox)
      2. Mascara o Poligono (QgsMapLayerComboBox)
      3. Opciones: cropToCutline, Buffer
      4. Valor NoData
      5. Salida (QgsFileWidget)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            "Recortar raster por zona de estudio - Geomaticape")
        self.resize(560, 380)
        self._build_ui()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        main = QVBoxLayout(self)
        main.setSpacing(10)
        main.setContentsMargins(15, 15, 15, 15)

        desc = QLabel(
            "<b>Recortar raster por zona de estudio</b><br>"
            "Recorta la imagen usando un poligono o mascara"
            "(vector o raster). Los nombres de banda se preservan en la salida."
        )
        desc.setWordWrap(True)
        main.addWidget(desc)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(14)

        # ── 1. Raster a Recortar ──────────────────────────────────────────
        self.cb_raster = QgsMapLayerComboBox()
        self.cb_raster.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        form.addRow("<b>Raster a Recortar</b>", self.cb_raster)

        # ── 2. Mascara o Poligono ─────────────────────────────────────────
        self.cb_mask = QgsMapLayerComboBox()
        self.cb_mask.setFilters(
            QgsMapLayerProxyModel.Filter.RasterLayer | QgsMapLayerProxyModel.Filter.PolygonLayer
        )
        form.addRow("<b>Mascara o Poligono</b>", self.cb_mask)

        # ── 3. Opciones de recorte ────────────────────────────────────────
        opts_w = QWidget()
        oh = QHBoxLayout(opts_w)
        oh.setContentsMargins(0, 0, 0, 0)
        oh.setSpacing(12)

        self.chk_crop = QCheckBox(
            "Uso del poligono segun forma geometría"
        )
        self.chk_crop.setChecked(True)

        lbl_buf = QLabel("Buffer:")
        self.spin_buffer = QDoubleSpinBox()
        self.spin_buffer.setDecimals(1)
        self.spin_buffer.setRange(0.0, 1e7)
        self.spin_buffer.setValue(0.0)
        self.spin_buffer.setFixedWidth(90)
        self.spin_buffer.setSuffix(" m")

        oh.addWidget(self.chk_crop)
        oh.addWidget(lbl_buf)
        oh.addWidget(self.spin_buffer)
        oh.addStretch(1)
        form.addRow("", opts_w)

        # ── 4. Valor NoData ───────────────────────────────────────────────
        nd_w = QWidget()
        nh = QHBoxLayout(nd_w)
        nh.setContentsMargins(0, 0, 0, 0)
        nh.setSpacing(4)
        self.chk_nodata = QCheckBox("Aplicar valor NoData:")
        self.chk_nodata.setChecked(False)
        self.spin_nodata = QDoubleSpinBox()
        self.spin_nodata.setDecimals(4)
        self.spin_nodata.setRange(-1e15, 1e15)
        self.spin_nodata.setValue(0.0)
        self.spin_nodata.setFixedWidth(120)
        self.spin_nodata.setEnabled(False)
        self.chk_nodata.toggled.connect(self.spin_nodata.setEnabled)
        nh.addWidget(self.chk_nodata)
        nh.addWidget(self.spin_nodata)
        nh.addStretch(1)
        form.addRow("<b>Valor NoData</b>", nd_w)

        # ── 5. Raster Salida Recortada ────────────────────────────────────
        self.fw_out = QgsFileWidget()
        self.fw_out.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.fw_out.setFilter("GeoTIFF (*.tif *.tiff)")
        self.fw_out.setDialogTitle("Guardar raster recortado")

        # Emular campo '[Guardar en archivo temporal]' nativo
        try:
            le = self.fw_out.findChild(QLineEdit)
            if le:
                le.setPlaceholderText("[Guardar en archivo temporal]")
        except BaseException:
            pass

        form.addRow("<b>Raster Salida Recortada</b>", self.fw_out)

        main.addLayout(form)
        main.addStretch(1)

        # ── OK / Cancel ───────────────────────────────────────────────────
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Ejecutar")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        main.addWidget(bb)

    # --------------------------------------------------------- Ejecutar

    def _on_ok(self):
        raster_lyr = self.cb_raster.currentLayer()
        if not raster_lyr:
            QMessageBox.warning(
                self, "Recortar",
                "Selecciona el raster a recortar en el panel de capas."
            )
            return

        mask_lyr = self.cb_mask.currentLayer()
        if not mask_lyr:
            QMessageBox.warning(
                self, "Recortar",
                "Selecciona la m&aacute;scara o pol&iacute;gono en el panel de capas."
            )
            return

        raster_path = raster_lyr.source()
        mask_is_raster = isinstance(mask_lyr, QgsRasterLayer)

        if mask_is_raster:
            mask_path = mask_lyr.source()
            mask_layer_name = ""
        else:
            mask_path = mask_lyr.source()
            mask_layer_name = None
            if "|" in mask_path:
                parts = mask_path.split("|")
                mask_path = parts[0]
                for p in parts[1:]:
                    if p.startswith("layername="):
                        mask_layer_name = p.split("=", 1)[1]

        out_path = self.fw_out.filePath().strip()
        if not out_path:
            out_path = os.path.join(
                tempfile.gettempdir(),
                f"_geomaticape_recorte_{os.getpid()}.tif"
            )
        else:
            if not out_path.lower().endswith((".tif", ".tiff")):
                out_path += ".tif"

        crop_to_cutline = self.chk_crop.isChecked()
        buffer_m = self.spin_buffer.value()
        nodata_value = (
            self.spin_nodata.value() if self.chk_nodata.isChecked() else None
        )
        compress = "LZW"

        progress = QProgressDialog(
            "Recortando raster...", "Cancelar", 0, 100, self
        )
        progress.setWindowTitle("Recortar raster por zona de estudio")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(True)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        feedback = _DialogFeedback(progress)
        try:
            result = ejecutar_recorte_simple(
                raster_path=raster_path,
                mask_path=mask_path,
                mask_is_raster=mask_is_raster,
                mask_layer_name=mask_layer_name,
                crop_to_cutline=crop_to_cutline,
                buffer_m=buffer_m,
                nodata_value=nodata_value,
                out_path=out_path,
                compress=compress,
                feedback=feedback,
            )
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Recortar \\u2014 Error", str(e))
            return

        progress.close()

        # Cargar resultado en QGIS si esta disponible
        try:
            lyr = QgsRasterLayer(result, os.path.basename(result))
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"No se pudo cargar '{result}' en el proyecto: {e}",
                "Geomaticape", Qgis.Warning)

        QMessageBox.information(
            self, "Recorte completado",
            f"Raster recortado guardado en:\n{result}"
        )
        self.accept()


# ---------------------------------------------------------------------------
# Wrapper invocado desde el menu Geomaticape -> Procesamiento
# ---------------------------------------------------------------------------


class RecortarRastersZona(GeomaticapeAlgorithm):
    """Lanzador desde el menu del plugin."""

    _algorithm_name = "recortar_rasters_zona"
    _icon_name = "poligonos_tabla.png"

    def __init__(self, iface=None):
        super().__init__()
        self.iface = iface

    def displayName(self):
        return self.tr("Recortar raster por zona de estudio")

    def group(self):
        return self.tr("Procesamiento")

    def groupId(self):
        return "geomaticape_procesamiento"

    def shortHelpString(self):
        return self.tr(
            "Herramienta interactiva para recortar rasters. Úsela desde el menú.")

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        raise QgsProcessingException(
            "Esta herramienta requiere interacción manual. Ejecútela desde el menú Geomaticape.")

    def icon(self):
        import os
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(
            os.path.dirname(__file__), "..", "Icons", self._icon_name
        ))

    def run(self):
        parent = None
        try:
            from qgis.utils import iface as _qgis_iface
            if _qgis_iface is not None:
                parent = _qgis_iface.mainWindow()
        except Exception:
            parent = None
        dlg = RecortarRastersZonaDialog(parent=parent)
        qt_exec(dlg)
