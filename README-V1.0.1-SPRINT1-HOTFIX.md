# Marinos AppBox Manager V1.0.1 — Sprint 1 Hotfix

Correctif de réactivité des commandes distantes.

- interrogation de la file de commandes toutes les 2 secondes ;
- heartbeat conservé à 60 secondes ;
- inventaire périodique à 30 secondes ;
- inventaire immédiat après chaque action AppBox ;
- suppression automatique de l’ancien drop-in systemd redondant.

Le délai de prise en charge d’une action ne dépend plus du heartbeat de 60 secondes.
