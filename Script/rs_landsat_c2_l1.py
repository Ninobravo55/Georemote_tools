from .geomaticape_algorithm import GeomaticapeAlgorithm
import os
import glob
import gc
import copy
import numpy as np
from qgis.core import (
    QgsProcessingParameterFile,
    QgsProcessingParameterEnum,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException, QgsMessageLog, Qgis
)
from qgis import processing
from osgeo import gdal


# =====================================================================
#  Reflectancia de Superficie + LST + PAN para Landsat Collection 2 L1
#  Detecta automaticamente el sensor: MSS, TM, ETM, OLI/TIRS
#  Salidas:
#     MSS       -> solo multiespectral
#     TM, ETM   -> multiespectral + termico (LST)
#     OLI/TIRS  -> multiespectral + termico (LST) + pancromatico
# =====================================================================


# ----- nombres de banda por sensor -----
def get_band_names(sensor):
    if "MSS" in sensor:
        return ["green", "red", "nir", "nir2"]
    elif "ETM" in sensor:
        return ["blue", "green", "red", "nir", "swir1", "swir2"]
    elif "TM" in sensor:
        return ["blue", "green", "red", "nir", "swir1", "swir2"]
    elif "OLI" in sensor:
        return ["coastal", "blue", "green", "red", "nir", "swir1", "swir2"]
    return []


# ----- lectura del MTL.txt -----
def read_mtl(path):
    d = {}
    with open(path) as f:
        for line in f:
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().replace('"', "")
                try:
                    d[k] = float(v)
                except Exception:
                    d[k] = v
    return d


# ----- Reflectancia de superficie con sustraccion de objeto oscuro -----
def calc_sr(img, M, A, sun_deg):
    toa = M * img + A
    toa = toa / np.sin(np.deg2rad(sun_deg))
    valid = toa[np.isfinite(toa) & (toa > 0)]
    dark = 0.0 if valid.size == 0 else float(np.percentile(valid, 1))
    sr = toa - dark
    return sr


# ----- Reflectancia de la banda PANCROMATICA -------------------------
# Los píxeles con DN=0 son relleno de borde (fill) del producto L1 y
# deben tratarse como nodata en la salida. Se convierten a NaN ANTES de
# llamar a esta función (en processAlgorithm) y aquí se preservan como
# NaN para que write_geotiff los escriba como nodata=-9999.
def calc_pan_reflectance(img, M, A, sun_deg, apply_dos=False, dos_pct=0.5):
    arr = img.astype(np.float32)       # los NaN de DN=0 se propagan
    toa = M * arr + A
    toa = toa / np.sin(np.deg2rad(sun_deg))

    if apply_dos:
        # Solo usar píxeles finitos y positivos para el dark object
        finite = toa[np.isfinite(toa) & (toa > 0)]
        if finite.size > 0:
            dark = float(np.percentile(finite, dos_pct))
            toa = toa - dark

    # Clipear valores negativos a 0 en píxeles válidos.
    # Preservar NaN (borde de escena) — NO reemplazar con 0.0.
    toa = np.where(np.isfinite(toa), np.clip(toa, 0.0, None), np.nan)
    return toa.astype(np.float32)


# ----- Land Surface Temperature -----
def calc_lst(img, ML, AL, K1, K2, emissivity=0.98):
    radiance = ML * img + AL
    radiance = np.where(radiance > 0, radiance, np.nan)
    bt = K2 / np.log((K1 / radiance) + 1)
    wavelength = 10.895e-6
    rho = 1.438e-2
    lst = bt / (1 + (wavelength * bt / rho) * np.log(emissivity))
    return lst - 273.15


# ----- limpieza de DN nulos (=0) -----
def clean_zeros(img):
    img = img.astype(np.float32)
    mask = copy.copy(img)
    mask[img >= 1] = 1.0
    mask[img < 1] = np.nan
    return mask * img


def write_geotiff(path, arr, profile_template, band_names, nodata=np.nan,
                  write_nodata=True):
    """Escribe un GeoTIFF Float32 multibanda con nombres internos.

    Si write_nodata=True (default) los NaN se reemplazan por -9999.0 y se
    declara NoData=-9999. Si write_nodata=False los NaN se reemplazan por
    0.0 y NO se declara NoData (todos los pixeles quedan como datos
    validos en la salida).
    """
    cols = profile_template["width"]
    rows = profile_template["height"]
    transform = profile_template["transform"]
    crs_wkt = profile_template["crs_wkt"]
    n = len(arr) if isinstance(arr, list) else 1

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        path, cols, rows, n, gdal.GDT_Float32,
        options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
    )
    ds.SetGeoTransform(transform)
    ds.SetProjection(crs_wkt)

    fill_val = -9999.0 if write_nodata else 0.0

    def _write_band(b, a, name):
        out = np.where(np.isnan(a), fill_val, a).astype(np.float32)
        b.WriteArray(out)
        b.SetDescription(name)
        if write_nodata:
            b.SetNoDataValue(-9999.0)
        else:
            try:
                b.DeleteNoDataValue()
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"No se pudo limpiar el NoData de la banda '{name}': {e}",
                    "Geomaticape", Qgis.Warning)

    if isinstance(arr, list):
        for i, a in enumerate(arr, start=1):
            _write_band(
                ds.GetRasterBand(i), a,
                band_names[i - 1] if i - 1 < len(band_names) else f"B{i}"
            )
    else:
        _write_band(
            ds.GetRasterBand(1), arr,
            band_names[0] if band_names else "B1"
        )

    ds.FlushCache()
    ds = None


def read_band_with_profile(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise QgsProcessingException(f"No se pudo abrir {path}")
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    profile = {
        "width": ds.RasterXSize,
        "height": ds.RasterYSize,
        "transform": ds.GetGeoTransform(),
        "crs_wkt": ds.GetProjection(),
    }
    ds = None
    return arr, profile


class RSLandSatC2L1(GeomaticapeAlgorithm):
    _algorithm_name = "rs_landsat_c2_l1"
    _icon_name = "landsat.png"

    INPUT_FOLDER = "INPUT_FOLDER"
    PAN_MODE = "PAN_MODE"
    OUTPUT_MS = "OUTPUT_MS"
    OUTPUT_THERMAL = "OUTPUT_THERMAL"
    OUTPUT_PAN = "OUTPUT_PAN"

    # Opciones para el modo de la banda PAN.
    PAN_MODE_OPTIONS = [
        "TOA reflectance (recomendado, preserva todos los pixeles)",
        "Surface Reflectance DOS1 conservador (puede oscurecer pixeles)",
    ]

    # -------------------------------------------------------
    # IDENTIFICACION
    # -------------------------------------------------------

    def displayName(self):
        return self.tr("Factor Escala LandSat C2 L1 (SR + LST + PAN)")

    def group(self):
        return self.tr("Conversion")

    def groupId(self):
        return "geomaticape_conversion"

    def shortHelpString(self):
        return """
<h3>Factor Escala LandSat C2 L1 — SR + LST + PAN</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.10<br><br>

<b>Descripcion:</b><br>
Procesa una escena Landsat <b>Collection 2 Level 1 (DN crudos)</b> aplicando:
<ul>
<li>Conversion DN -&gt; Radiancia espectral (TOA): L = M*DN + A</li>
<li>Correccion atmosferica DOS1 (sustraccion de objeto oscuro, percentil 1)
    para las bandas <b>multiespectrales</b></li>
<li>Reflectancia de Superficie por banda multiespectral</li>
<li>Land Surface Temperature en grados Celsius para la banda termica</li>
<li><b>Pancromatica</b>: TOA reflectance (sin DOS por defecto). Los píxeles
    de borde del producto (DN=0) se enmascaran como <b>nodata=-9999</b>
    y aparecen <b>transparentes</b> en QGIS. Opcionalmente DOS1 conservador.</li>
</ul>

El sensor se detecta <b>automaticamente</b> leyendo el archivo
<code>*MTL.txt</code> de la escena. Las salidas dependen del sensor:

<table border="1" cellpadding="6" cellspacing="0">
<tr><th>Sensor</th><th>Multiespectral</th><th>Termico (LST)</th><th>Pancromatico</th></tr>
<tr><td>MSS  (L1-L5)</td><td align="center">si</td><td align="center">no</td><td align="center">no</td></tr>
<tr><td>TM   (L4-L5)</td><td align="center">si</td><td align="center">si (B6)</td><td align="center">no</td></tr>
<tr><td>ETM+ (L7)</td><td align="center">si</td><td align="center">si (B6_VCID_2)</td><td align="center">no</td></tr>
<tr><td>OLI/TIRS (L8-L9)</td><td align="center">si</td><td align="center">si (B10)</td><td align="center">si (B8)</td></tr>
</table>

<b>Bandas de salida (multiespectral, automaticas):</b>
<ul>
<li>MSS: green, red, nir, nir2</li>
<li>TM / ETM+: blue, green, red, nir, swir1, swir2</li>
<li>OLI: coastal, blue, green, red, nir, swir1, swir2</li>
</ul>

<b>Requisito:</b> carpeta de la escena Landsat Collection 2 L1 con su
archivo <code>*_MTL.txt</code> y todas las bandas <code>*B*.TIF</code>.<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    # -------------------------------------------------------
    # PARAMETROS
    # -------------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_FOLDER,
                self.tr("Carpeta de la escena Landsat Collection 2 Level 1"),
                behavior=QgsProcessingParameterFile.Behavior.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.PAN_MODE,
                self.tr("Modo de la banda Pancromatica (solo OLI)"),
                options=self.PAN_MODE_OPTIONS,
                defaultValue=0
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_MS,
                self.tr("Multiespectral SR (siempre se genera)")
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_THERMAL,
                self.tr("Termico LST (TM / ETM / OLI - omitir si MSS)"),
                optional=True,
                createByDefault=True
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_PAN,
                self.tr(
                    "Pancromatico SR (solo OLI / TIRS - omitir en otros sensores)"),
                optional=True,
                createByDefault=True
            )
        )

    # -------------------------------------------------------
    # PROCESO
    # -------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        folder = self.parameterAsString(parameters, self.INPUT_FOLDER, context)
        pan_mode = self.parameterAsEnum(parameters, self.PAN_MODE, context)
        out_ms = self.parameterAsOutputLayer(
            parameters, self.OUTPUT_MS, context)
        out_thermal = self.parameterAsOutputLayer(
            parameters, self.OUTPUT_THERMAL, context)
        out_pan = self.parameterAsOutputLayer(
            parameters, self.OUTPUT_PAN, context)

        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("La carpeta indicada no existe.")

        # ---------------------------------------------------
        # 1. LEER MTL Y DETECTAR SENSOR
        # ---------------------------------------------------
        mtl_list = glob.glob(os.path.join(folder, "*MTL.txt"))
        if not mtl_list:
            raise QgsProcessingException(
                "No se encontro archivo *_MTL.txt en la carpeta. "
                "Esta herramienta requiere una escena Landsat C2 L1 completa."
            )

        mtl_path = mtl_list[0]
        feedback.pushInfo(f"MTL detectado: {os.path.basename(mtl_path)}")
        p = read_mtl(mtl_path)

        sensor = p.get("SENSOR_ID", "")
        sun = p.get("SUN_ELEVATION", 0)

        if not sensor:
            raise QgsProcessingException(
                "No se pudo leer SENSOR_ID en el MTL.")

        feedback.pushInfo(f"Sensor detectado: {sensor}")
        feedback.pushInfo(f"SUN_ELEVATION   : {sun}")

        # ---------------------------------------------------
        # 2. LISTAR BANDAS Y CLASIFICAR POR ROL
        # ---------------------------------------------------
        candidatos = [b for b in glob.glob(os.path.join(folder, "*B*.TIF"))
                      if "_SR" not in b.upper()
                      and "_LST" not in b.upper()
                      and "_PAN" not in b.upper()]

        ms_bands = []
        pan_band = None
        thermal_band = None

        sensor_up = sensor.upper()
        for b in candidatos:
            if 'feedback' in locals() and feedback.isCanceled():
                break
            name = os.path.basename(b).upper()

            if "MSS" in sensor_up:
                ms_bands.append(b)

            elif "ETM" in sensor_up:
                if "B6_VCID_2" in name:
                    thermal_band = b
                elif "B6_VCID_1" in name:
                    # nos quedamos con VCID_2; ignorar VCID_1
                    continue
                elif any(f"_B{i}." in name for i in [1, 2, 3, 4, 5, 7]):
                    ms_bands.append(b)

            elif "TM" in sensor_up:
                if "_B6." in name:
                    thermal_band = b
                elif any(f"_B{i}." in name for i in [1, 2, 3, 4, 5, 7]):
                    ms_bands.append(b)

            elif "OLI" in sensor_up:
                if "_B8." in name:
                    pan_band = b
                elif "_B10." in name:
                    thermal_band = b
                elif "_B11." in name:
                    continue
                elif "_B9." in name:
                    # banda cirrus, no se exporta como MS
                    continue
                elif any(f"_B{i}." in name for i in range(1, 8)):
                    ms_bands.append(b)

        ms_bands = sorted(ms_bands, key=lambda x: int(
            os.path.basename(x).upper().split("_B")[-1].split(".")[0]
        ))

        feedback.pushInfo(
            f"Bandas multiespectrales encontradas: {
                len(ms_bands)}")
        for b in ms_bands:
            if 'feedback' in locals() and feedback.isCanceled():
                break
            feedback.pushInfo(f"  MS  -> {os.path.basename(b)}")
        if thermal_band:
            feedback.pushInfo(f"  TIR -> {os.path.basename(thermal_band)}")
        if pan_band:
            feedback.pushInfo(f"  PAN -> {os.path.basename(pan_band)}")

        if not ms_bands:
            raise QgsProcessingException(
                f"No se encontraron bandas multiespectrales para el sensor {sensor}."
            )

        band_names = get_band_names(sensor)
        feedback.setProgress(10)

        # ---------------------------------------------------
        # 3. PROCESAR MULTIESPECTRAL (SR DOS1)
        # ---------------------------------------------------
        feedback.pushInfo(
            "Procesando reflectancia de superficie multiespectral...")
        stack = []
        ref_profile = None

        for b in ms_bands:
            if 'feedback' in locals() and feedback.isCanceled():
                break
            band_num = int(os.path.basename(
                b).upper().split("_B")[-1].split(".")[0])

            if "MSS" in sensor_up:
                M = p.get(f"REFLECTANCE_MULT_BAND_{band_num}")
                A = p.get(f"REFLECTANCE_ADD_BAND_{band_num}")
            else:
                M = p.get(f"REFLECTANCE_MULT_BAND_{band_num}")
                A = p.get(f"REFLECTANCE_ADD_BAND_{band_num}")

            if M is None or A is None:
                feedback.pushInfo(
                    f"  Saltando banda B{band_num} (sin coeficientes)")
                continue

            arr, profile = read_band_with_profile(b)
            arr = clean_zeros(arr)
            sr = calc_sr(arr, float(M), float(A), float(sun))

            valid = sr[np.isfinite(sr)]
            if valid.size > 0:
                feedback.pushInfo(
                    f"  B{band_num} min={
                        valid.min():.4f} max={
                        valid.max():.4f}")
            stack.append(sr)

            if ref_profile is None:
                ref_profile = profile

            feedback.setProgress(
                10 + int((len(stack) / max(len(ms_bands), 1)) * 50))

        if not stack:
            raise QgsProcessingException(
                "No se pudo procesar ninguna banda multiespectral.")

        write_geotiff(out_ms, stack, ref_profile, band_names)
        feedback.pushInfo(f"  -> Multiespectral SR: {out_ms}")
        feedback.setProgress(60)

        # ---------------------------------------------------
        # 4. PROCESAR TERMICO (LST) - solo TM / ETM / OLI
        # ---------------------------------------------------
        if thermal_band and out_thermal:
            feedback.pushInfo("Procesando termico (LST)...")
            try:
                if "OLI" in sensor_up:
                    ML = p["RADIANCE_MULT_BAND_10"]
                    AL = p["RADIANCE_ADD_BAND_10"]
                    K1 = p["K1_CONSTANT_BAND_10"]
                    K2 = p["K2_CONSTANT_BAND_10"]
                elif "ETM" in sensor_up:
                    ML = p["RADIANCE_MULT_BAND_6_VCID_2"]
                    AL = p["RADIANCE_ADD_BAND_6_VCID_2"]
                    K1 = p["K1_CONSTANT_BAND_6_VCID_2"]
                    K2 = p["K2_CONSTANT_BAND_6_VCID_2"]
                elif "TM" in sensor_up:
                    ML = p["RADIANCE_MULT_BAND_6"]
                    AL = p["RADIANCE_ADD_BAND_6"]
                    K1 = p["K1_CONSTANT_BAND_6"]
                    K2 = p["K2_CONSTANT_BAND_6"]
                else:
                    raise Exception(f"Sensor sin termica: {sensor}")

                arr, profile = read_band_with_profile(thermal_band)
                arr = clean_zeros(arr)
                lst = calc_lst(arr, float(ML), float(AL), float(K1), float(K2))

                valid = lst[np.isfinite(lst)]
                if valid.size > 0:
                    feedback.pushInfo(
                        f"  LST min={
                            valid.min():.2f} C  max={
                            valid.max():.2f} C")

                write_geotiff(out_thermal, lst, profile, ["LST_Celsius"])
                feedback.pushInfo(f"  -> LST: {out_thermal}")

            except Exception as e:
                feedback.pushInfo(f"  AVISO: no se pudo generar LST ({e})")
        elif thermal_band and not out_thermal:
            feedback.pushInfo("Saltando LST (no se asigno ruta de salida).")
        else:
            feedback.pushInfo(
                f"Sensor {sensor} sin banda termica - omitiendo LST.")

        feedback.setProgress(80)

        # ---------------------------------------------------
        # 5. PROCESAR PANCROMATICO - solo OLI
        # ---------------------------------------------------
        if pan_band and out_pan:
            apply_dos_pan = (pan_mode == 1)
            modo_txt = ("TOA reflectance (sin DOS)" if not apply_dos_pan
                        else "Surface Reflectance DOS1 (conservador)")
            feedback.pushInfo(
                f"Procesando pancromatico (B8) - modo: {modo_txt}")
            band_num = int(os.path.basename(
                pan_band).upper().split("_B")[-1].split(".")[0])
            M = p.get(f"REFLECTANCE_MULT_BAND_{band_num}")
            A = p.get(f"REFLECTANCE_ADD_BAND_{band_num}")

            if M is not None and A is not None:
                arr, profile = read_band_with_profile(pan_band)

                # ── Enmascarar DN=0 del borde del producto (fill) ──────────
                # DN=0 en el producto L1 es relleno (fill) del área fuera de
                # la escena. Se convierte a NaN ANTES de la conversión TOA
                # para que esos píxeles salgan como nodata en el GeoTIFF
                # y aparezcan transparentes en QGIS (no como valor 0).
                arr = arr.astype(np.float32)
                arr[arr == 0] = np.nan

                sr_pan = calc_pan_reflectance(
                    arr, float(M), float(A), float(sun),
                    apply_dos=apply_dos_pan,
                    dos_pct=0.5,
                )

                valid = sr_pan[np.isfinite(sr_pan) & (sr_pan > 0)]
                if valid.size > 0:
                    feedback.pushInfo(
                        f"  B{band_num} (PAN, {modo_txt}) "
                        f"min={valid.min():.4f}  max={valid.max():.4f}  "
                        f"píxeles válidos={valid.size}"
                    )
                else:
                    feedback.pushInfo(
                        f"  AVISO: B{band_num} (PAN) no produjo píxeles > 0"
                    )

                # Escribir CON nodata=-9999: los DN=0 originales (borde)
                # quedan enmascarados y se ven transparentes en QGIS.
                write_geotiff(out_pan, sr_pan, profile, ["Pan"],
                              write_nodata=True)
                feedback.pushInfo(f"  -> Pancromatico: {out_pan}")
            else:
                feedback.pushInfo(
                    "  AVISO: B8 sin coeficientes de reflectancia, se omite PAN.")
        elif pan_band and not out_pan:
            feedback.pushInfo("Saltando PAN (no se asigno ruta de salida).")
        else:
            feedback.pushInfo(
                f"Sensor {sensor} sin banda pancromatica - omitiendo PAN.")

        feedback.setProgress(100)

        # Limpieza
        del stack
        gc.collect()

        feedback.pushInfo("PROCESO RS LANDSAT C2 L1 COMPLETADO")

        results = {self.OUTPUT_MS: out_ms}
        if thermal_band and out_thermal and os.path.exists(out_thermal):
            results[self.OUTPUT_THERMAL] = out_thermal
        if pan_band and out_pan and os.path.exists(out_pan):
            results[self.OUTPUT_PAN] = out_pan
        return results

    def run(self):
        processing.execAlgorithmDialog(self)
