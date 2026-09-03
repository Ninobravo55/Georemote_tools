"""
Combinar bandas con nombres
============================
Dialogo Qt personalizado para apilar bandas de varios raster en un GeoTIFF
multibanda con nombres descriptivos (Red, NIR, SWIR1...). El nombre se
edita AL COSTADO de cada raster en la columna "Nombre de la banda".

Fuentes admitidas:
  * Archivos raster del disco (boton "Agregar raster...").
  * Capas raster del proyecto QGIS (boton "Agregar capa(s) QGIS...").

Para cada entrada se elige la BANDA del raster a usar (combo "Banda" en
la fila). Por defecto banda 1; si el origen tiene N bandas, el combo
permite escoger 1..N. El nombre por defecto se autodetecta desde
band[N].GetDescription() o, si esta vacio, el nombre del archivo.

Si los raster no comparten grilla / CRS / extent, los reproyecta y
remuestrea automaticamente al primero (raster de referencia) usando
gdal.Warp con el metodo elegido por el usuario.

Cada banda se etiqueta con band.SetDescription(nombre) en el GeoTIFF
final, de modo que QGIS, ArcGIS, ENVI y SNAP muestran "Red, NIR, ..."
en lugar de "Band 1, Band 2, ...".

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.14
Grupo : Procesamiento
"""

from ._qt_compat import qt_exec
from qgis.core import QgsProcessingException, QgsMessageLog, Qgis
from .geomaticape_algorithm import GeomaticapeAlgorithm
import os
import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton, QLabel, QLineEdit,
    QFileDialog, QMessageBox, QComboBox, QDialogButtonBox,
    QProgressDialog, QApplication, QWidget, QAbstractItemView,
)
from osgeo import gdal


RESAMPLE_METHODS = [
    "nearest", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode",
]
COMPRESS_OPTIONS = ["LZW", "DEFLATE", "PACKBITS", "ZSTD", "NONE"]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _safe_name(s):
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(s)).strip("_")
    return s or "Band"


def _grid_info(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL no pudo abrir: {path}")
    info = {
        "path": path,
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "gt": ds.GetGeoTransform(),
        "proj": ds.GetProjection(),
        "nbands": ds.RasterCount,
    }
    ds = None
    return info


def _grids_match(a, b, tol=1e-6):
    if a["cols"] != b["cols"] or a["rows"] != b["rows"]:
        return False
    if (a["proj"] or "") != (b["proj"] or ""):
        return False
    for i in range(6):
        if abs(a["gt"][i] - b["gt"][i]) > tol:
            return False
    return True


def _band_count(path):
    """Cuenta bandas con GDAL (1 si falla)."""
    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is not None:
            n = ds.RasterCount
            ds = None
            return max(1, int(n))
    except Exception as e:
        QgsMessageLog.logMessage(
            f"No se pudo contar bandas de '{path}': {e}",
            "Geomaticape", Qgis.Warning)
    return 1


def _detect_band_name_at(path, band_idx):
    """Lee la descripcion de la banda N; None si no hay."""
    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is not None:
            if 1 <= band_idx <= ds.RasterCount:
                b = ds.GetRasterBand(band_idx)
                desc = b.GetDescription() if b is not None else ""
                ds = None
                if desc:
                    return _safe_name(desc)
            ds = None
    except Exception as e:
        QgsMessageLog.logMessage(
            f"No se pudo leer el nombre de la banda {band_idx} de "
            f"'{path}': {e}",
            "Geomaticape", Qgis.Warning)
    return None


def _default_name(path, band_idx=1, layer_name=None):
    """Nombre por defecto para una entrada raster + banda."""
    n = _detect_band_name_at(path, band_idx)
    if n:
        return n
    if layer_name:
        if band_idx == 1:
            return _safe_name(layer_name)
        return _safe_name(f"{layer_name}_b{band_idx}")
    base = os.path.splitext(os.path.basename(path))[0]
    if band_idx == 1:
        return _safe_name(base)
    return _safe_name(f"{base}_b{band_idx}")


def _smart_default_name(path, band_idx=1, layer_name=None):
    """Detecta automaticamente nombres comunes de bandas Landsat y Sentinel-2."""

    # 1. Intentar obtener el nombre interno guardado en el archivo (metadatos)
    n = _detect_band_name_at(path, band_idx)
    # GDAL a veces devuelve "Band 1", lo ignoramos para usar la deteccion
    # inteligente
    if n and not re.match(r"^Band\s*\d+$", n, re.IGNORECASE):
        return n

    # 2. Si no hay nombre interno descriptivo, usamos el nombre del archivo
    name_up = (layer_name or os.path.basename(path)).upper()

    if "LC08" in name_up or "LC09" in name_up:
        if "_B1." in name_up or "_B1_" in name_up or name_up.endswith("_B1"):
            return "Coastal/Aerosol"
        if "_B2." in name_up or "_B2_" in name_up or name_up.endswith("_B2"):
            return "Blue"
        if "_B3." in name_up or "_B3_" in name_up or name_up.endswith("_B3"):
            return "Green"
        if "_B4." in name_up or "_B4_" in name_up or name_up.endswith("_B4"):
            return "Red"
        if "_B5." in name_up or "_B5_" in name_up or name_up.endswith("_B5"):
            return "NIR"
        if "_B6." in name_up or "_B6_" in name_up or name_up.endswith("_B6"):
            return "SWIR1"
        if "_B7." in name_up or "_B7_" in name_up or name_up.endswith("_B7"):
            return "SWIR2"
        if "_B10." in name_up or "_B10_" in name_up or name_up.endswith(
                "_B10"):
            return "Thermal 1"
        if "_B11." in name_up or "_B11_" in name_up or name_up.endswith(
                "_B11"):
            return "Thermal 2"

    elif "S2A" in name_up or "S2B" in name_up or "SENTINEL" in name_up:
        if "B01" in name_up:
            return "Aerosol"
        if "B02" in name_up:
            return "Blue"
        if "B03" in name_up:
            return "Green"
        if "B04" in name_up:
            return "Red"
        if "B05" in name_up:
            return "Red Edge 1"
        if "B06" in name_up:
            return "Red Edge 2"
        if "B07" in name_up:
            return "Red Edge 3"
        if "B08" in name_up:
            return "NIR"
        if "B8A" in name_up:
            return "Red Edge 4"
        if "B09" in name_up:
            return "Water vapor"
        if "B11" in name_up:
            return "SWIR1"
        if "B12" in name_up:
            return "SWIR2"

    # 3. Fallback final al nombre de la capa o archivo
    if layer_name:
        if band_idx == 1:
            return _safe_name(layer_name)
        return _safe_name(f"{layer_name}_b{band_idx}")
    base = os.path.splitext(os.path.basename(path))[0]
    if band_idx == 1:
        return _safe_name(base)
    return _safe_name(f"{base}_b{band_idx}")


def _cleanup(paths):
    for p in paths:
        if not p:
            continue
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Logica de procesamiento
# ---------------------------------------------------------------------------

def ejecutar_combinacion(paths, bands, names, out_path, resample, compress,
                         feedback):
    """Apila paths[bands] en out_path con nombres dados, alineando al primero."""
    if not paths or len(paths) < 2:
        raise RuntimeError("Selecciona al menos 2 raster.")
    if not (len(paths) == len(bands) == len(names)):
        raise RuntimeError(
            "Listas inconsistentes (paths/bands/names tienen distinta longitud)."
        )

    ref = _grid_info(paths[0])
    if not ref["proj"]:
        feedback.pushWarning(
            "Raster de referencia sin CRS; la salida tampoco tendra CRS."
        )

    feedback.pushInfo("=" * 60)
    feedback.pushInfo("Combinar bandas con nombres")
    feedback.pushInfo(
        f"Referencia: {os.path.basename(paths[0])}  "
        f"({ref['cols']}x{ref['rows']} px)"
    )
    feedback.pushInfo("Orden y nombres:")
    for i, (p, b, n) in enumerate(zip(paths, bands, names), 1):
        feedback.pushInfo(
            f"  Banda {i:2d}: {n:<14s}  <-  {os.path.basename(p)} "
            f"(banda origen: {b})"
        )
    feedback.pushInfo("=" * 60)

    out_dir = os.path.dirname(out_path) or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)

    ref_xmin = ref["gt"][0]
    ref_ymax = ref["gt"][3]
    ref_xmax = ref_xmin + ref["cols"] * ref["gt"][1]
    ref_ymin = ref_ymax + ref["rows"] * ref["gt"][5]
    ref_xres = abs(ref["gt"][1])
    ref_yres = abs(ref["gt"][5])

    aligned_paths = []
    tmp_files = []
    n = len(paths)
    for k, (p, bnd) in enumerate(zip(paths, bands), start=1):
        if feedback.isCanceled():
            break
        info = _grid_info(p)
        if bnd < 1 or bnd > info["nbands"]:
            feedback.pushWarning(
                f"  '{os.path.basename(p)}': banda {bnd} fuera de rango "
                f"(tiene {info['nbands']}). Se usa banda 1."
            )
            bnd = 1

        # Caso rapido: archivo de 1 banda, banda 1, grilla identica.
        if info["nbands"] == 1 and bnd == 1 and _grids_match(info, ref):
            aligned_paths.append(p)
            feedback.pushInfo(
                f"  [{k}/{n}] Grilla OK -> {os.path.basename(p)} (banda 1)"
            )
        else:
            tmp = os.path.join(out_dir, f"_geomaticape_align_{k:02d}.tif")
            tmp_files.append(tmp)
            try:
                gdal.Warp(
                    tmp, p, format="GTiff", srcBands=[bnd],
                    dstSRS=ref["proj"] or None,
                    outputBounds=(ref_xmin, ref_ymin, ref_xmax, ref_ymax),
                    xRes=ref_xres, yRes=ref_yres,
                    width=ref["cols"], height=ref["rows"],
                    resampleAlg=resample, multithread=True,
                    creationOptions=[
                        "TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"
                    ],
                )
            except TypeError:
                tmp_b1 = tmp + ".b1.tif"
                tmp_files.append(tmp_b1)
                gdal.Translate(tmp_b1, p, bandList=[bnd])
                gdal.Warp(
                    tmp, tmp_b1, format="GTiff",
                    dstSRS=ref["proj"] or None,
                    outputBounds=(ref_xmin, ref_ymin, ref_xmax, ref_ymax),
                    xRes=ref_xres, yRes=ref_yres,
                    width=ref["cols"], height=ref["rows"],
                    resampleAlg=resample, multithread=True,
                    creationOptions=[
                        "TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"
                    ],
                )
            aligned_paths.append(tmp)
            feedback.pushInfo(
                f"  [{k}/{n}] Alineado: {os.path.basename(p)} (banda {bnd}) "
                f"-> banda {k} ({names[k - 1]})"
            )
        feedback.setProgress(int(k * 80 / n))

    if feedback.isCanceled():
        _cleanup(tmp_files)
        raise RuntimeError("Operacion cancelada por el usuario.")

    feedback.pushInfo("Construyendo VRT multibanda...")
    vrt_path = os.path.splitext(out_path)[0] + "_stack.vrt"
    vrt_opts = gdal.BuildVRTOptions(
        separate=True, resolution="user",
        xRes=ref_xres, yRes=ref_yres,
        outputBounds=(ref_xmin, ref_ymin, ref_xmax, ref_ymax),
    )
    vrt_ds = gdal.BuildVRT(vrt_path, aligned_paths, options=vrt_opts)
    if vrt_ds is None:
        _cleanup(tmp_files + [vrt_path])
        raise RuntimeError("No se pudo construir el VRT multibanda.")
    vrt_ds = None  # flush

    feedback.pushInfo("Escribiendo GeoTIFF final...")
    creation = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if compress != "NONE":
        creation.append(f"COMPRESS={compress}")
    gdal.Translate(out_path, vrt_path, creationOptions=creation)
    feedback.setProgress(95)

    ds_out = gdal.Open(out_path, gdal.GA_Update)
    if ds_out is None:
        _cleanup(tmp_files + [vrt_path])
        raise RuntimeError(f"No se pudo abrir el raster final: {out_path}")
    for i, nm in enumerate(names, 1):
        band = ds_out.GetRasterBand(i)
        band.SetDescription(nm)
        try:
            band.SetMetadataItem("BAND_NAME", nm)
        except Exception as e:
            feedback.pushInfo(
                f"Aviso: no se pudo etiquetar la banda {i} ('{nm}'): {e}")
    try:
        ds_out.SetMetadataItem("GEOMATICAPE_BAND_ORDER", ",".join(names))
    except Exception as e:
        feedback.pushInfo(
            f"Aviso: no se pudo escribir el orden de bandas en metadatos: {e}")
    ds_out.FlushCache()
    ds_out = None

    _cleanup(tmp_files + [vrt_path])

    feedback.pushInfo("=" * 60)
    feedback.pushInfo(f"OK - Raster combinado: {out_path}")
    feedback.pushInfo(f"Bandas: {', '.join(names)}")
    feedback.pushInfo("=" * 60)
    feedback.setProgress(100)
    return out_path


# ---------------------------------------------------------------------------
# Adaptador de feedback contra QProgressDialog
# ---------------------------------------------------------------------------

class _DialogFeedback:
    def __init__(self, progress=None):
        self.progress = progress
        self.log = []

    def _emit(self, msg):
        self.log.append(msg)
        if self.progress:
            self.progress.setLabelText(msg[:160])
            QApplication.processEvents()

    def pushInfo(self, msg):
        self._emit(str(msg))

    def pushWarning(self, msg):
        self._emit("AVISO: " + str(msg))

    def setProgress(self, pct):
        if self.progress:
            self.progress.setValue(int(pct))
            QApplication.processEvents()

    def isCanceled(self):
        return bool(self.progress and self.progress.wasCanceled())


# ---------------------------------------------------------------------------
# Selectores y Dialogos auxiliares
# ---------------------------------------------------------------------------

class _BandSelectionDialog(QDialog):
    """Dialogo para elegir que bandas agregar de un raster multibanda."""

    def __init__(self, filepath, nbands, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Seleccionar bandas")
        self.filepath = filepath
        self.nbands = nbands
        self.selected_bands = []
        self._build_ui()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.addWidget(
            QLabel(
                f"El raster tiene {
                    self.nbands} bandas.\n¿Cuales deseas agregar?\n\n{
                    os.path.basename(
                        self.filepath)}"))

        # Scroll area for checkboxes if there are many bands
        from qgis.PyQt.QtWidgets import QScrollArea, QCheckBox
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        w = QWidget()
        self.lay_checks = QVBoxLayout(w)

        self.checks = []
        for i in range(1, self.nbands + 1):
            chk = QCheckBox(f"Banda {i}")
            chk.setChecked(True)  # Seleccionadas por defecto
            self.lay_checks.addWidget(chk)
            self.checks.append(chk)

        self.lay_checks.addStretch(1)
        scroll.setWidget(w)
        v.addWidget(scroll)

        # Botones All / None
        hb = QHBoxLayout()
        btn_all = QPushButton("Todas")
        btn_none = QPushButton("Ninguna")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        hb.addWidget(btn_all)
        hb.addWidget(btn_none)
        v.addLayout(hb)

        # Ok/Cancel
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _set_all(self, state):
        for c in self.checks:
            c.setChecked(state)

    def get_selected(self):
        return [i + 1 for i, c in enumerate(self.checks) if c.isChecked()]


class _QGISLayerPickerDialog(QDialog):
    """Dialogo modal para escoger capas raster del proyecto QGIS y la
    banda a usar de cada una."""

    HEADERS = ("Capa raster del proyecto", "# bandas", "Banda a usar")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agregar bandas desde capas QGIS")
        self.resize(680, 440)
        self._build_ui()
        self._populate()

    def _build_ui(self):
        v = QVBoxLayout(self)

        info = QLabel(
            "<b>Marca</b> las capas raster que quieras agregar y elige la "
            "<b>banda</b> a usar de cada una. Cada capa marcada agregara "
            "una fila en la tabla principal."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        self.tbl = QTableWidget(0, 3, self)
        self.tbl.setHorizontalHeaderLabels(self.HEADERS)
        self.tbl.verticalHeader().setVisible(False)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.tbl, 1)

        # Botones de seleccion masiva
        bar = QHBoxLayout()
        self.btn_check_all = QPushButton("Marcar todas")
        self.btn_check_all.clicked.connect(lambda: self._check_all(Qt.CheckState.Checked))
        self.btn_uncheck_all = QPushButton("Desmarcar todas")
        self.btn_uncheck_all.clicked.connect(
            lambda: self._check_all(Qt.CheckState.Unchecked))
        bar.addWidget(self.btn_check_all)
        bar.addWidget(self.btn_uncheck_all)
        bar.addStretch(1)
        v.addLayout(bar)

        self.lbl_status = QLabel("")
        v.addWidget(self.lbl_status)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Agregar marcadas")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _populate(self):
        self.tbl.setRowCount(0)
        layers = []
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsRasterLayer) and lyr.isValid():
                    layers.append(lyr)
        except Exception as e:
            self.lbl_status.setText(
                f"No fue posible leer las capas QGIS: {e}"
            )
            return

        if not layers:
            self.lbl_status.setText(
                "(No hay capas raster cargadas en el proyecto QGIS.)"
            )
            return

        for lyr in layers:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)

            try:
                nb = int(lyr.bandCount())
            except Exception:
                nb = 1
            nb = max(1, nb)

            it_name = QTableWidgetItem(lyr.name())
            it_name.setFlags(it_name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it_name.setCheckState(Qt.CheckState.Unchecked)
            it_name.setData(Qt.ItemDataRole.UserRole, lyr.source())
            it_name.setData(Qt.ItemDataRole.UserRole + 1, lyr.name())
            it_name.setData(Qt.ItemDataRole.UserRole + 2, nb)
            it_name.setToolTip(lyr.source())
            self.tbl.setItem(r, 0, it_name)

            it_nb = QTableWidgetItem(str(nb))
            it_nb.setFlags(it_nb.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it_nb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tbl.setItem(r, 1, it_nb)

            combo = QComboBox()
            if nb > 1:
                combo.addItem("Todas")
            for i in range(1, nb + 1):
                combo.addItem(str(i))
            combo.setCurrentIndex(0)
            self.tbl.setCellWidget(r, 2, combo)

        self.lbl_status.setText(
            f"{len(layers)} capa(s) raster encontradas en el proyecto."
        )

    def _check_all(self, state):
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is not None:
                it.setCheckState(state)

    def get_selected(self):
        """Lista de dicts: source_path, source_label, nbands, band_idx, default_name."""
        out = []
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is None or it.checkState() != Qt.CheckState.Checked:
                continue
            src = it.data(Qt.ItemDataRole.UserRole) or ""
            lname = it.data(Qt.ItemDataRole.UserRole + 1) or it.text()
            nb = int(it.data(Qt.ItemDataRole.UserRole + 2) or 1)
            combo = self.tbl.cellWidget(r, 2)
            sel_text = combo.currentText() if combo else "1"

            if sel_text == "Todas":
                bands_to_add = list(range(1, nb + 1))
            else:
                bands_to_add = [int(sel_text)]

            for b_idx in bands_to_add:
                label = f"{lname} (capa QGIS)"
                default_name = _smart_default_name(
                    src, b_idx, layer_name=lname)
                out.append({
                    "source_path": src,
                    "source_label": label,
                    "nbands": nb,
                    "band_idx": b_idx,
                    "name": default_name,
                })
        return out


# ---------------------------------------------------------------------------
# Dialogo principal
# ---------------------------------------------------------------------------

class CombinarBandasNombresDialog(QDialog):

    COLS = ("#", "Origen (capa o archivo)", "Banda", "Nombre de la banda")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Combinar bandas con nombres - Geomaticape")
        self.resize(900, 560)
        self.setAcceptDrops(True)
        self._build_ui()

    # ------------------- UI ---------------------------------------------

    def _build_ui(self):
        v = QVBoxLayout(self)

        info = QLabel(
            "<b>Combinar bandas con nombres</b><br>"
            "Agrega <b>archivos del disco</b> o <b>capas raster del "
            "proyecto QGIS</b>. Para cada entrada elige la <b>banda</b> "
            "(combo en la columna <i>Banda</i>) y edita el <b>nombre</b> "
            "directamente al costado en la columna <i>Nombre de la "
            "banda</i> (doble click o F2). El orden de las filas (Subir / "
            "Bajar) define el orden de las bandas en el GeoTIFF de salida."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        # Tabla principal: 4 columnas
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, 240)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Habilitar arrastrar y soltar en la tabla indirectamente via Dialog
        self.table.setAcceptDrops(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        v.addWidget(self.table, 1)

        # Botonera
        row = QHBoxLayout()
        self.btn_add_file = QPushButton("Agregar raster (archivo)...")
        self.btn_add_file.clicked.connect(self._on_add_file)
        self.btn_add_qgis = QPushButton("Agregar capa(s) QGIS...")
        self.btn_add_qgis.clicked.connect(self._on_add_qgis)
        self.btn_remove = QPushButton("Quitar")
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_up = QPushButton("Subir")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = QPushButton("Bajar")
        self.btn_down.clicked.connect(lambda: self._move(+1))
        self.btn_clear = QPushButton("Limpiar")
        self.btn_clear.clicked.connect(self._on_clear)
        for b in (self.btn_add_file, self.btn_add_qgis, self.btn_remove,
                  self.btn_up, self.btn_down, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        # Opciones + salida
        f = QFormLayout()

        out_widget = QWidget()
        oh = QHBoxLayout(out_widget)
        oh.setContentsMargins(0, 0, 0, 0)
        self.line_out = QLineEdit()
        self.btn_out = QPushButton("...")
        self.btn_out.setFixedWidth(34)
        self.btn_out.clicked.connect(self._on_out)
        oh.addWidget(self.line_out, 1)
        oh.addWidget(self.btn_out)
        f.addRow("Raster de salida (.tif):", out_widget)

        v.addLayout(f)

        # OK/Cancel
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Ejecutar")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # ------------------- helpers de filas --------------------------------

    def _append_row(self, source_path, source_label, nbands, band_idx, name):
        nb = max(1, int(nbands))
        bi = max(1, min(int(band_idx), nb))

        r = self.table.rowCount()
        self.table.insertRow(r)

        # Col 0: #
        it_n = QTableWidgetItem(str(r + 1))
        it_n.setFlags(it_n.flags() & ~Qt.ItemFlag.ItemIsEditable)
        it_n.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(r, 0, it_n)

        # Col 1: Origen
        it_p = QTableWidgetItem(source_label)
        it_p.setToolTip(source_path)
        it_p.setData(Qt.ItemDataRole.UserRole, source_path)
        it_p.setData(Qt.ItemDataRole.UserRole + 1, nb)
        it_p.setFlags(it_p.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(r, 1, it_p)

        # Col 2: Banda combo
        combo = QComboBox()
        for i in range(1, nb + 1):
            combo.addItem(str(i))
        combo.setCurrentIndex(bi - 1)
        self.table.setCellWidget(r, 2, combo)

        # Col 3: Nombre editable
        it_name = QTableWidgetItem(name)
        it_name.setToolTip("Doble click para editar el nombre de la banda")
        self.table.setItem(r, 3, it_name)

        # Foco en nombre para edicion rapida
        self.table.setCurrentCell(r, 3)
        try:
            self.table.editItem(it_name)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"No se pudo abrir el editor de nombre de banda: {e}",
                "Geomaticape", Qgis.Warning)

    def _snapshot_rows(self):
        rows = []
        for r in range(self.table.rowCount()):
            it_p = self.table.item(r, 1)
            combo = self.table.cellWidget(r, 2)
            it_n = self.table.item(r, 3)
            if it_p is None:
                continue
            rows.append({
                "source_path": it_p.data(Qt.ItemDataRole.UserRole) or "",
                "source_label": it_p.text() or "",
                "nbands": int(it_p.data(Qt.ItemDataRole.UserRole + 1) or 1),
                "band_idx": int(combo.currentText()) if combo else 1,
                "name": it_n.text() if it_n else "",
            })
        return rows

    def _populate_from_rows(self, rows):
        self.table.setRowCount(0)
        for d in rows:
            self._append_row(
                d["source_path"], d["source_label"],
                d["nbands"], d["band_idx"], d["name"]
            )

    # ------------------- eventos de arrastrar y soltar -------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        files = []
        for u in urls:
            if u.isLocalFile():
                f = u.toLocalFile()
                if f.lower().endswith(('.tif', '.tiff', '.img', '.vrt', '.jp2', '.dat')):
                    files.append(f)

        self._process_dropped_files(files)

    def _process_dropped_files(self, files):
        for f in files:
            nb = _band_count(f)
            if nb > 1:
                dlg = _BandSelectionDialog(f, nb, self)
                if qt_exec(dlg) == QDialog.DialogCode.Accepted:
                    bands_to_add = dlg.get_selected()
                else:
                    bands_to_add = []  # cancelo
            else:
                bands_to_add = [1]

            for b_idx in bands_to_add:
                self._append_row(
                    source_path=f,
                    source_label=os.path.basename(f),
                    nbands=nb,
                    band_idx=b_idx,
                    name=_smart_default_name(f, b_idx),
                )

        # Auto-rellenar salida si esta vacia
        if files and self.table.rowCount() > 0 and not self.line_out.text().strip():
            primer = files[0]
            base, ext = os.path.splitext(primer)
            self.line_out.setText(f"{base}_stack{ext}")

    # ------------------- acciones ----------------------------------------

    def _on_add_file(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecciona uno o mas raster",
            "",
            "Raster (*.tif *.tiff *.img *.vrt *.jp2 *.dat);;Todos (*.*)"
        )
        if files:
            self._process_dropped_files(files)

    def _on_add_qgis(self):
        dlg = _QGISLayerPickerDialog(parent=self)
        if qt_exec(dlg) != QDialog.DialogCode.Accepted:
            return
        items = dlg.get_selected()
        if not items:
            QMessageBox.information(
                self, "Combinar bandas",
                "No marcaste ninguna capa."
            )
            return
        for d in items:
            self._append_row(
                source_path=d["source_path"],
                source_label=d["source_label"],
                nbands=d["nbands"],
                band_idx=d["band_idx"],
                name=d["name"],
            )

    def _on_remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._renumber()

    def _on_clear(self):
        self.table.setRowCount(0)

    def _move(self, delta):
        sel_rows = sorted(
            {i.row() for i in self.table.selectedIndexes()},
            reverse=(delta > 0)
        )
        if not sel_rows:
            return
        rows = self._snapshot_rows()
        n = len(rows)
        moved = []
        for r in sel_rows:
            new_r = r + delta
            if 0 <= new_r < n and new_r not in moved:
                rows[r], rows[new_r] = rows[new_r], rows[r]
                moved.append(new_r)
            else:
                moved.append(r)
        self._populate_from_rows(rows)
        self.table.clearSelection()
        for r in moved:
            self.table.selectRow(r)

    def _renumber(self):
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is None:
                it = QTableWidgetItem()
                self.table.setItem(r, 0, it)
            it.setText(str(r + 1))
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _on_out(self):
        start = self.line_out.text().strip()
        f, _ = QFileDialog.getSaveFileName(
            self, "Raster combinado de salida", start,
            "GeoTIFF (*.tif *.tiff)"
        )
        if f:
            if not f.lower().endswith((".tif", ".tiff")):
                f += ".tif"
            self.line_out.setText(f)

    # ------------------- ejecutar ----------------------------------------

    def _gather(self):
        rows = self._snapshot_rows()
        paths, bands, names = [], [], []
        for d in rows:
            p = d["source_path"]
            if not p:
                continue
            n = (d["name"] or "").strip()
            if not n:
                n = _smart_default_name(p, d["band_idx"])
            paths.append(p)
            bands.append(int(d["band_idx"]))
            names.append(_safe_name(n))
        return paths, bands, names

    def _on_ok(self):
        # Forzar fin de edicion en curso
        cur = self.table.currentItem()
        if cur is not None:
            try:
                self.table.closePersistentEditor(cur)
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"No se pudo cerrar el editor de tabla en curso: {e}",
                    "Geomaticape", Qgis.Warning)

        if self.table.rowCount() < 2:
            QMessageBox.warning(self, "Combinar bandas",
                                "Agrega al menos 2 raster.")
            return
        out_path = self.line_out.text().strip()
        if not out_path:
            QMessageBox.warning(self, "Combinar bandas",
                                "Define el raster de salida (.tif).")
            return
        if not out_path.lower().endswith((".tif", ".tiff")):
            out_path += ".tif"
            self.line_out.setText(out_path)

        paths, bands, names = self._gather()

        # Aviso por nombres duplicados
        seen = set()
        dup = set()
        for n in names:
            if n in seen:
                dup.add(n)
            seen.add(n)
        if dup:
            r = QMessageBox.question(
                self, "Combinar bandas",
                "Hay nombres de banda duplicados: "
                + ", ".join(sorted(dup))
                + ".\nContinuar de todos modos?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        resample = "nearest"
        compress = "LZW"

        progress = QProgressDialog("Procesando...", "Cancelar", 0, 100, self)
        progress.setWindowTitle("Combinar bandas con nombres")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(True)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        feedback = _DialogFeedback(progress)
        try:
            ejecutar_combinacion(paths, bands, names, out_path,
                                 resample, compress, feedback)
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Combinar bandas - Error", str(e))
            return

        progress.close()

        # Cargar resultado en QGIS si esta disponible
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            lyr = QgsRasterLayer(out_path, os.path.basename(out_path))
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"No se pudo cargar '{out_path}' en el proyecto: {e}",
                "Geomaticape", Qgis.Warning)

        QMessageBox.information(
            self, "Combinar bandas",
            "Raster combinado generado:\n"
            + out_path
            + "\n\nBandas:\n  "
            + "\n  ".join(
                f"{i + 1}. {n}  (banda origen: {b})"
                for i, (n, b) in enumerate(zip(names, bands))
            )
        )
        self.accept()


# ---------------------------------------------------------------------------
# Wrapper invocado desde el menu Geomaticape -> Procesamiento
# ---------------------------------------------------------------------------


class CombinarBandasNombres(GeomaticapeAlgorithm):
    """Lanzador desde el menu del plugin y stub para Processing."""

    _algorithm_name = "combinar_bandas_nombres"
    _icon_name = "combinar_bandas.png"

    def __init__(self, iface=None):
        super().__init__()
        self.iface = iface

    def displayName(self):
        return self.tr("Combinar bandas con nombres (Red, NIR, SWIR1...)")

    def group(self):
        return self.tr("Procesamiento")

    def groupId(self):
        return "geomaticape_procesamiento"

    def shortHelpString(self):
        return self.tr(
            "Herramienta interactiva para apilar bandas. Úsela desde el menú.")

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        raise QgsProcessingException(
            "Esta herramienta requiere interacción manual. Ejecútela desde el menú Geomaticape.")

    def icon(self):
        import os
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(
            __file__), "..", "Icons", self._icon_name))

    def run(self):
        parent = None
        try:
            from qgis.utils import iface as _qgis_iface
            if _qgis_iface is not None:
                parent = _qgis_iface.mainWindow()
        except Exception:
            parent = None
        dlg = CombinarBandasNombresDialog(parent=parent)
        qt_exec(dlg)
