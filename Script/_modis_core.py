# -*- coding: utf-8 -*-
"""
_modis_core.py — Núcleo compartido para herramientas MODIS
Geomaticape Plugin
Autor: GEOMATICA AMBIENTAL  |  Version: 1.3

Lee subdatasets HDF4 con gdal.Open(sds_path) directamente
(el sds_path viene de GetSubDatasets() y gdal lo abre sin problemas
aunque rioxarray falle con ese string en Windows).

Flujo:
  1. gdal.Open(hdf)  → GetSubDatasets()  → lista de (path_sds, desc)
  2. gdal.Open(path_sds)  → ReadAsArray() + GetGeoTransform() + GetProjection()
  3. Validar rango DN válido (-100 … 16000) antes del factor de escala
  4. Aplicar factor / offset  (numpy)
  5. Escribir GeoTIFF temporal en proyección original  (gdal CreateCopy)
  6. Reproyectar a EPSG:4326 con gdal.Warp
  7. Guardar en carpeta de salida
"""

# Rango DN válido para reflectancia superficial MODIS 09
# Valores fuera de este rango (y distintos al nodata) se enmascaran
import numpy as np
import tempfile
import gc
import os
DN_MIN_VALIDO = -100
DN_MAX_VALIDO = 16000


# ------------------------------------------------------------------
def listar_sds(ruta_hdf, feedback):
    """
    Retorna lista de (path_sds, descripcion) usando gdal.
    Nunca lanza excepción — devuelve [] y loguea si falla.
    """
    try:
        from osgeo import gdal
        gdal.UseExceptions()
        ds = gdal.Open(ruta_hdf, gdal.GA_ReadOnly)
        if ds is None:
            feedback.reportError(
                f"  ⚠ gdal no pudo abrir: {ruta_hdf}\n"
                "     Verifica que el HDF4 no esté dañado o incompleto.", False
            )
            return []
        sds = ds.GetSubDatasets()   # [(path, desc), ...]
        ds = None
        return sds   # lista completa con path Y descripción
    except Exception as e:
        feedback.reportError(f"  ⚠ Error leyendo subdatasets: {e}", False)
        return []


# ------------------------------------------------------------------
def verificar_rango_dn(arr, nodata_src, feedback, nombre_sds="",
                       dn_min=None, dn_max=None):
    """
    Verifica que los valores DN del array estén dentro del rango válido
    especificado por dn_min / dn_max.

    Si no se proporcionan, usa los valores por defecto del módulo
    (DN_MIN_VALIDO / DN_MAX_VALIDO, definidos para MODIS 09).

    - Excluye píxeles nodata de la comprobación.
    - Reporta estadísticas: min, max, % fuera de rango.
    - Retorna una máscara booleana (True = fuera de rango y no es nodata).
    """
    rango_min = DN_MIN_VALIDO if dn_min is None else dn_min
    rango_max = DN_MAX_VALIDO if dn_max is None else dn_max

    # Construir máscara de píxeles de datos reales (excluir nodata)
    if nodata_src is not None:
        datos_validos = arr != nodata_src
    else:
        datos_validos = np.ones(arr.shape, dtype=bool)

    arr_datos = arr[datos_validos]
    total_datos = arr_datos.size

    if total_datos == 0:
        feedback.pushInfo(
            f"     ℹ️  {nombre_sds}: sin píxeles de datos (todo nodata)")
        return np.zeros(arr.shape, dtype=bool)

    dn_min_real = arr_datos.min()
    dn_max_real = arr_datos.max()

    fuera_rango = datos_validos & ((arr < rango_min) | (arr > rango_max))
    n_fuera = int(fuera_rango.sum())
    pct_fuera = (n_fuera / total_datos) * 100.0

    # ── Reporte de estadísticas ────────────────────────────────────
    estado = "✔" if n_fuera == 0 else "⚠"
    feedback.pushInfo(
        f"     {estado} DN rango real: [{
            dn_min_real:.0f}, {
            dn_max_real:.0f}]  "
        f"| Válido: [{rango_min}, {rango_max}]  "
        f"| Fuera de rango: {n_fuera} px ({pct_fuera:.2f}%)"
    )

    if n_fuera > 0:
        dn_fuera_vals = arr[fuera_rango]
        feedback.reportError(
            f"     ⚠ {nombre_sds}: {n_fuera} píxel(es) con DN inválido  "
            f"(min={dn_fuera_vals.min():.0f}, max={dn_fuera_vals.max():.0f})  "
            f"→ serán enmascarados como nodata",
            False
        )

    return fuera_rango


# ------------------------------------------------------------------
def procesar_banda(
    # path completo del subdataset (gdal lo abre nativamente)
    sds_path,
    ruta_tif_out,       # destino final GeoTIFF
    factor,             # escala multiplicativa (float)
    offset,             # offset aditivo post-factor (float)
    nodata_out,         # valor nodata en la salida
    dtype_out,          # 'float32' | 'uint8'
    resample_nn,        # True → vecino más cercano (categórico)
    feedback,
    # límite inferior del rango DN válido (None = usar defecto del módulo)
    dn_min=None,
    # límite superior del rango DN válido (None = usar defecto del módulo)
    dn_max=None,
):
    """
    Lee el subdataset con gdal, aplica factor+offset, reproyecta a
    EPSG:4326 con gdal.Warp y guarda el GeoTIFF final.
    Retorna True si tuvo éxito, False si falló.
    """
    try:
        from osgeo import gdal
        gdal.UseExceptions()

        # ── 1. Abrir subdataset ────────────────────────────────
        sds_ds = gdal.Open(sds_path, gdal.GA_ReadOnly)
        if sds_ds is None:
            feedback.reportError(
                f"     ⚠ gdal no pudo abrir el subdataset:\n"
                f"       {sds_path}", False
            )
            return False

        banda = sds_ds.GetRasterBand(1)
        arr = banda.ReadAsArray().astype("float64")
        gt = sds_ds.GetGeoTransform()
        proj = sds_ds.GetProjection()
        nrows, ncols = arr.shape

        # Nodata original (puede ser None si no está definido en el HDF)
        nodata_src = banda.GetNoDataValue()
        sds_ds = None

        # ── 2. Máscara de nodata original ──────────────────────
        if nodata_src is not None:
            mascara_nodata = (arr == nodata_src)
        else:
            mascara_nodata = None

        # ── 3. Validar rango DN válido antes del factor de escala ─
        nombre_sds = sds_path.split(":")[-1] if ":" in sds_path else sds_path
        mascara_fuera_rango = verificar_rango_dn(
            arr=arr,
            nodata_src=nodata_src,
            feedback=feedback,
            nombre_sds=nombre_sds,
            dn_min=dn_min,
            dn_max=dn_max,
        )

        # ── 4. Aplicar factor + offset ─────────────────────────
        if factor != 1.0 or offset != 0.0:
            arr = arr * factor + offset

        # Restaurar nodata: píxeles originales nodata + píxeles fuera de rango
        if mascara_nodata is not None:
            arr[mascara_nodata] = nodata_out
        arr[mascara_fuera_rango] = nodata_out

        arr = arr.astype(dtype_out)

        # ── 3. Escribir GeoTIFF temporal en proyección original ─
        gdal_dtype = (
            gdal.GDT_Byte if dtype_out == "uint8"
            else gdal.GDT_Float32
        )

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tif", prefix="modis_tmp_")
        os.close(tmp_fd)

        driver = gdal.GetDriverByName("GTiff")
        tmp_ds = driver.Create(tmp_path, ncols, nrows, 1, gdal_dtype, options=[
                               "COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"])
        tmp_ds.SetGeoTransform(gt)
        tmp_ds.SetProjection(proj)
        b_out = tmp_ds.GetRasterBand(1)
        b_out.SetNoDataValue(float(nodata_out))
        b_out.WriteArray(arr)
        tmp_ds.FlushCache()
        tmp_ds = None
        del arr

        # ── 4. Reproyectar a EPSG:4326 con gdal.Warp ──────────
        resample_alg = (
            gdal.GRA_NearestNeighbour if resample_nn
            else gdal.GRA_Bilinear
        )

        warp_opts = gdal.WarpOptions(
            format="GTiff",
            dstSRS="EPSG:4326",
            dstNodata=nodata_out,
            resampleAlg=resample_alg,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES",
                             "BIGTIFF=IF_SAFER"],
        )

        gdal.Warp(ruta_tif_out, tmp_path, options=warp_opts)

        # ── 5. Limpieza ────────────────────────────────────────
        try:
            os.remove(tmp_path)
        except Exception as e:
            feedback.pushInfo(
                f"     ⚠ No se pudo borrar el temporal '{tmp_path}': {e}")

        gc.collect()
        return True

    except Exception as e:
        feedback.reportError(f"     ⚠ Error en procesar_banda: {e}", False)
        try:
            if 'tmp_path' in dir() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception as e2:
            feedback.pushInfo(
                f"     ⚠ No se pudo borrar el temporal tras el error: {e2}")
        gc.collect()
        return False
