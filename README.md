# SFTP HE1

Aplicación Flask para buscar en Oracle las carpetas asociadas a un `DIG_ID_TRAMITE`, descargarlas desde SFTP y empaquetarlas en ZIP.

## Acceso

- Usuario: `Leticia`
- Clave: `12345678`

## Ejecución sin Docker

1. Crear y activar un entorno virtual:

```bash
python3 -m venv .venv
. .venv/bin/activate
```

2. Instalar dependencias:

```bash
pip install -r requirements.txt
```

3. Preparar `.env`:

```bash
cp .env.example .env
```

4. Verificar que el driver exista en:

```text
jdbc/ojdbc8.jar
```

5. Iniciar la app:

```bash
python run_local.py
```

También puedes usar:

```bash
./run_local.sh
```

Por defecto escucha en `127.0.0.1:5085`. Puedes cambiarlo con `APP_HOST` y `APP_PORT` en `.env`.

## Como servicio systemd

La unidad usa `gunicorn`, arranca al boot de Ubuntu y se reinicia sola si el proceso cae.

1. Copiar `sftp_he1.service` a `/etc/systemd/system/`.
2. Ejecutar:

```bash
sudo ./install_systemd.sh
```

La URL sigue siendo la misma, normalmente `http://127.0.0.1:5085` o la que tenga tu proxy delante.

## Como servicio de usuario

Si no quieres usar `sudo`, puedes instalarlo como servicio de usuario:

```bash
./install_user_service.sh
```

Eso deja el servicio activo en tu sesión y con reinicio automático si falla.
Si quieres que sobreviva a reinicios del equipo sin iniciar sesión, un admin debe ejecutar:

```bash
loginctl enable-linger red_gestion
```

## Variables importantes

- `ORACLE_JDBC_JAR`: ruta al `ojdbc8.jar`.
- `ORACLE_TARGETS`: lista `host:puerto:sid` separada por comas.
- `SFTP_REMOTE_BASE`: base remota del árbol SFTP.
- `LOCAL_REPO_ROOT`: raíz local del repositorio para la pantalla de sincronización por `ANIOMES`.
- `DOWNLOAD_OUTPUT_ROOT`: carpeta local donde se guardan los ZIP y manifests.
- `APP_HOST` y `APP_PORT`: host y puerto del servidor Flask.

## Pantalla de sincronización

En `/sync-candidates` puedes ingresar un `ANIOMES` como `202604` para filtrar Oracle por `FE_PLA_ANIOMES` y comparar el repositorio local entre `HSP03` y `HSP04`.
Primero haces un dry run desde la web y, si el resultado te cuadra, ejecutas la sincronización desde el mismo panel.

## Descarga optimizada

En la pantalla principal, el botón **Iniciar descarga optimizada** copia directamente desde `LOCAL_REPO_ROOT` solo las carpetas del trámite seleccionado y genera el ZIP sin pasar por una sincronización completa del árbol fuente.
Esto evita volver a bajar todo el `SRC` cuando solo necesitas un `DIG_ID_TRAMITE` puntual.

## Validación de rutas

En `/path-validation` puedes ingresar un `ANIOMES` como `202604` para comparar el `path` esperado desde Oracle contra el `path` real encontrado en el repositorio.
La pantalla muestra solo las discrepancias, calcula porcentajes y permite hacer dry run antes de mover con `rsync`.

## Verificación previa a producción

1. Copiar `.env.production.example` a `.env`.
2. Ajustar credenciales y rutas reales.
3. Ejecutar:

```bash
./check_production.sh
```

Si todo está bien, el script confirma que existen el `.env`, el `ojdbc8.jar` y el entorno virtual.

## Limpieza de PDFs

En `/admin` hay un botón para borrar todos los archivos `.pdf` generados dentro de `output/`.
Eso ayuda a liberar espacio si el repositorio local acumula PDFs viejos.
