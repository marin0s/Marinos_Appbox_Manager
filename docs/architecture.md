# Architecture

AppBox Manager utilise un modèle control plane / agents.

Le Control Plane sur CRONOS conserve l'état métier, les jobs, les décisions de placement et la file de commandes. Les agents interrogent l'API, exécutent localement les opérations Docker puis renvoient leur résultat et leur inventaire.

Le runtime observé par les agents est la source de vérité pour l'état des conteneurs. La base conserve l'état désiré et les informations métier. Le moteur de réconciliation compare les deux.

Les communications sont initiées par les agents vers le Control Plane. Aucun accès Docker distant direct depuis CRONOS n'est requis.
