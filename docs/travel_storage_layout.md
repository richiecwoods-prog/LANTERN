# LANTERN Travel Storage Layout

Use GitHub for source code and a separate folder for runtime data.

## Recommended Windows Layout

```text
C:\Projects\LANTERN      Git clone of richiecwoods-prog/LANTERN
C:\LANTERN-data          Runtime data copied from the old H: working copy
H:\EEI APPS\MOTH         Old working copy and backup reference
```

## Runtime Data Folder

`C:\LANTERN-data` should contain:

```text
moth.sqlite
uploads\
```

Optional archive folders can sit beside it:

```text
C:\LANTERN-archive\backups
C:\LANTERN-archive\reports
C:\LANTERN-archive\scans
C:\LANTERN-archive\releases
```

## Launching Against External Data

From the cloned repository:

```powershell
.\Start_LANTERN_Local.ps1 -DataRoot C:\LANTERN-data
```

or:

```powershell
.\LANTERN_App.ps1 -DataRoot C:\LANTERN-data
```

The launcher sets `MOTH_DATA_DIR`, `MOTH_DB_PATH`, and `MOTH_UPLOAD_DIR` for the app process, so the repository stays clean while the active database and uploads live outside Git.
