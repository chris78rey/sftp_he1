# Guía para levantar y administrar servicios

Este proyecto se ejecuta como servicio `systemd` sobre Ubuntu.

La unidad principal es:

```text
sftp_he1.service
```

La app corre con `gunicorn` y se expone normalmente en:

```text
http://127.0.0.1:5085
```

## 1. Ubicación del servicio

- Archivo de unidad: [`sftp_he1.service`](./sftp_he1.service)
- Directorio de trabajo: [`/data_nuevo/flask_sftp/sftp_he1`](./)
- Variables de entorno: [`/data_nuevo/flask_sftp/sftp_he1/.env`](./.env.example)
- Binario de arranque: [`/data_nuevo/flask_sftp/sftp_he1/.venv/bin/gunicorn`](./.venv/bin/gunicorn)

## 2. Verificar estado

```bash
sudo systemctl status sftp_he1.service --no-pager
```

Comandos útiles:

```bash
sudo systemctl is-active sftp_he1.service
sudo systemctl is-enabled sftp_he1.service
sudo systemctl show sftp_he1.service -p MainPID
```

## 3. Arrancar el servicio

```bash
sudo systemctl start sftp_he1.service
```

Si quieres reiniciarlo al mismo tiempo:

```bash
sudo systemctl restart sftp_he1.service
```

## 4. Detener el servicio

```bash
sudo systemctl stop sftp_he1.service
```

## 5. Reiniciar el servicio

Cuando cambias código, templates o `.env`:

```bash
sudo systemctl restart sftp_he1.service
```

Si el servicio se quedó colgado, primero revisa el estado y luego reinicia:

```bash
sudo systemctl status sftp_he1.service --no-pager
sudo systemctl restart sftp_he1.service
```

## 6. Habilitar al arranque

Para que el servicio suba solo cuando arranca el sistema:

```bash
sudo systemctl enable sftp_he1.service
```

Para quitarlo del arranque automático:

```bash
sudo systemctl disable sftp_he1.service
```

## 7. Ver logs

Ver las últimas líneas:

```bash
sudo journalctl -u sftp_he1.service -n 50 --no-pager
```

Seguir logs en tiempo real:

```bash
sudo journalctl -u sftp_he1.service -f
```

Ver solo errores recientes:

```bash
sudo journalctl -u sftp_he1.service -p err -n 50 --no-pager
```

## 8. Recargar configuración

Si cambias el archivo de unidad `sftp_he1.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl restart sftp_he1.service
```

## 9. Revisión de archivos clave

Antes de levantar el servicio, valida que existan:

```bash
ls -lah .env
ls -lah .venv/bin/gunicorn
ls -lah jdbc/ojdbc8.jar
```

También puedes verificar la configuración efectiva:

```bash
systemctl cat sftp_he1.service
```

## 10. Flujos de operación

### Descarga principal

Desde la pantalla principal:

- buscar por `DIG_ID_TRAMITE` o `DIG_TRAMITE`
- usar `Iniciar descarga optimizada`
- revisar el detalle del trabajo
- descargar el ZIP desde la interfaz

### Hospitalización y urgencias

Desde la web:

- `HOSPITALIZACION CASOS`
- `URGENCIAS CASOS`

Ahí se ejecuta el flujo por `ANIOMES` y se puede hacer dry run antes de mover.

### Validación de rutas

Desde `VALIDACION RUTAS`:

- se revisa Oracle contra el repositorio local
- se ven discrepancias
- se puede descargar el CSV parcial

## 11. Ruta de salida de trabajos

Los ZIP y manifests quedan en:

```text
/data_nuevo/flask_sftp/sftp_he1/output
```

Desde `Admin` puedes revisar las descargas recientes y bajar los ZIP.

## 12. Problemas comunes

### El servicio no arranca

Revisa:

```bash
sudo systemctl status sftp_he1.service --no-pager
sudo journalctl -u sftp_he1.service -n 100 --no-pager
```

### Cambié `.env` y no se ve reflejado

Reinicia el servicio:

```bash
sudo systemctl restart sftp_he1.service
```

### No carga la web

Verifica que el puerto esté escuchando:

```bash
ss -ltnp | grep 5085
```

## 13. Servicio de usuario

Si en lugar de system-wide quieres ejecutarlo como servicio de usuario:

```bash
./install_user_service.sh
```

Ese modo usa:

```text
~/.config/systemd/user/sftp_he1.service
```

## 14. Notas operativas

- No usar `kill -9` salvo emergencia.
- Si el proceso cae, preferir `systemctl restart`.
- Si cambian templates o Python, reiniciar siempre el servicio.
- Si cambian `sftp_he1.service`, hacer `daemon-reload`.

