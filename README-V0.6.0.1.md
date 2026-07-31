# Marinos AppBox Manager V0.6.0.1

Correctif critique de démarrage de la V0.6.0.

La migration initiale des Volume Mounts omettait `node_id` dans les paramètres
SQLite. Uvicorn quittait pendant l'événement startup, avant l'ouverture du port 8090.

Cette version corrige le seed SQLite et ajoute un upgrade avec rollback automatique.
