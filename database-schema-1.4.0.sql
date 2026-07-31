CREATE TABLE nodes (
            node_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'local',
            status TEXT NOT NULL DEFAULT 'online',
            maintenance INTEGER NOT NULL DEFAULT 0,
            docker_version TEXT,
            agent_version TEXT NOT NULL DEFAULT 'embedded-0.4.0',
            rdad_ok INTEGER NOT NULL DEFAULT 0,
            gpu_ok INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE appboxes (
            client_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'plex',
            with_tautulli INTEGER NOT NULL DEFAULT 0,
            plex_port INTEGER,
            tautulli_port INTEGER,
            status TEXT NOT NULL DEFAULT 'generated',
            path TEXT NOT NULL,
            containers_json TEXT NOT NULL,
            last_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, profile_id TEXT, snapshot_id TEXT, mount_group_id TEXT, storage_mode TEXT NOT NULL DEFAULT 'independent', port_mode TEXT NOT NULL DEFAULT 'automatic', plex_username TEXT, reference_image_id TEXT, reference_version_id TEXT, acceleration_mode TEXT NOT NULL DEFAULT 'auto', placement_mode TEXT NOT NULL DEFAULT 'manual', requested_node_id TEXT, selected_node_id TEXT, placement_reason TEXT, desired_state TEXT NOT NULL DEFAULT 'running', observed_state TEXT NOT NULL DEFAULT 'unknown', reconciliation_status TEXT NOT NULL DEFAULT 'unknown', drift_json TEXT NOT NULL DEFAULT '[]', reconciled_at TEXT, protection_level TEXT NOT NULL DEFAULT 'standard', archived_at TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );
CREATE UNIQUE INDEX ux_appbox_node_plex_port
            ON appboxes(node_id, plex_port) WHERE plex_port IS NOT NULL;
CREATE UNIQUE INDEX ux_appbox_node_tautulli_port
            ON appboxes(node_id, tautulli_port) WHERE tautulli_port IS NOT NULL;
CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            client_id TEXT,
            node_id TEXT NOT NULL,
            action TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            queue_position INTEGER, options_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id)
        );
CREATE INDEX ix_jobs_queue
            ON jobs(status, created_at);
CREATE INDEX ix_jobs_client
            ON jobs(client_id, created_at DESC);
CREATE TABLE job_steps (
            step_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            step_key TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            started_at TEXT,
            finished_at TEXT, executor TEXT NOT NULL DEFAULT 'control-plane', resources_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            node_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT,
            created_at TEXT NOT NULL
        );
CREATE TABLE node_metrics (
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            cpu_percent REAL,
            load_1 REAL,
            ram_percent REAL,
            ram_used INTEGER,
            ram_total INTEGER,
            disk_percent REAL,
            disk_free INTEGER,
            disk_read_bps REAL,
            disk_write_bps REAL,
            net_rx_bps REAL,
            net_tx_bps REAL,
            docker_containers INTEGER,
            running_containers INTEGER,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );
CREATE INDEX ix_metrics_node_time
            ON node_metrics(node_id, collected_at DESC);
CREATE UNIQUE INDEX ux_job_steps_job_key
            ON job_steps(job_id, step_key);
CREATE TABLE containers (
            container_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            appbox_id TEXT,
            name TEXT NOT NULL,
            image TEXT,
            image_id TEXT,
            state TEXT NOT NULL DEFAULT 'unknown',
            status TEXT,
            health TEXT,
            restart_count INTEGER NOT NULL DEFAULT 0,
            ports_json TEXT NOT NULL DEFAULT '[]',
            labels_json TEXT NOT NULL DEFAULT '{}',
            mounts_json TEXT NOT NULL DEFAULT '[]',
            networks_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT,
            last_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(appbox_id) REFERENCES appboxes(client_id)
        );
CREATE INDEX ix_containers_node
            ON containers(node_id, name);
CREATE INDEX ix_containers_appbox
            ON containers(appbox_id, name);
CREATE TABLE networks (
            network_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            appbox_id TEXT,
            name TEXT NOT NULL,
            driver TEXT,
            scope TEXT,
            internal INTEGER NOT NULL DEFAULT 0,
            attachable INTEGER NOT NULL DEFAULT 0,
            labels_json TEXT NOT NULL DEFAULT '{}',
            containers_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT,
            last_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(appbox_id) REFERENCES appboxes(client_id)
        );
CREATE INDEX ix_networks_node
            ON networks(node_id, name);
CREATE INDEX ix_networks_appbox
            ON networks(appbox_id, name);
CREATE TABLE volumes (
            volume_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            appbox_id TEXT,
            name TEXT NOT NULL,
            driver TEXT,
            mountpoint TEXT,
            scope TEXT,
            labels_json TEXT NOT NULL DEFAULT '{}',
            options_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT,
            last_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(appbox_id) REFERENCES appboxes(client_id)
        );
CREATE INDEX ix_volumes_node
            ON volumes(node_id, name);
CREATE INDEX ix_volumes_appbox
            ON volumes(appbox_id, name);
CREATE TABLE templates (
            template_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '1',
            enabled INTEGER NOT NULL DEFAULT 1,
            definition_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE port_reservations (
            reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            client_id TEXT,
            service TEXT NOT NULL,
            port INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT 'tcp',
            status TEXT NOT NULL DEFAULT 'reserved',
            reserved_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id)
        );
CREATE UNIQUE INDEX ux_port_reservations_active
            ON port_reservations(node_id, port, protocol)
            WHERE status='reserved';
CREATE INDEX ix_port_reservations_client
            ON port_reservations(client_id, service);
CREATE TABLE settings_store (
            setting_key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            updated_at TEXT NOT NULL
        );
CREATE TABLE notifications_queue (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            node_id TEXT,
            channel TEXT NOT NULL DEFAULT 'internal',
            level TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            last_error TEXT,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );
CREATE INDEX ix_notifications_queue_status
            ON notifications_queue(status, created_at);
CREATE TABLE storage_mounts (
            mount_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            node_id TEXT NOT NULL,
            host_path TEXT NOT NULL,
            container_path TEXT NOT NULL,
            read_only INTEGER NOT NULL DEFAULT 1,
            propagation TEXT NOT NULL DEFAULT 'rprivate',
            required INTEGER NOT NULL DEFAULT 0,
            media_types_json TEXT NOT NULL DEFAULT '["plex","jellyfin"]',
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );
CREATE UNIQUE INDEX ux_storage_mount_path
            ON storage_mounts(node_id, host_path, container_path);
CREATE TABLE mount_groups (
            group_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE mount_group_members (
            group_id TEXT NOT NULL,
            mount_id TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(group_id,mount_id),
            FOREIGN KEY(group_id) REFERENCES mount_groups(group_id) ON DELETE CASCADE,
            FOREIGN KEY(mount_id) REFERENCES storage_mounts(mount_id) ON DELETE CASCADE
        );
CREATE TABLE catalog_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '1',
            source_path TEXT,
            checksum TEXT,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            expected_paths_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE provisioning_profiles (
            profile_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            snapshot_id TEXT,
            mount_group_id TEXT,
            storage_mode TEXT NOT NULL DEFAULT 'independent',
            is_blank INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, reference_image_id TEXT, reference_version_id TEXT, acceleration_mode TEXT NOT NULL DEFAULT 'auto',
            FOREIGN KEY(snapshot_id) REFERENCES catalog_snapshots(snapshot_id),
            FOREIGN KEY(mount_group_id) REFERENCES mount_groups(group_id)
        );
CREATE TABLE appbox_mounts (
            client_id TEXT NOT NULL,
            mount_id TEXT NOT NULL,
            host_path TEXT NOT NULL,
            container_path TEXT NOT NULL,
            read_only INTEGER NOT NULL,
            propagation TEXT NOT NULL,
            PRIMARY KEY(client_id,mount_id),
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id) ON DELETE CASCADE,
            FOREIGN KEY(mount_id) REFERENCES storage_mounts(mount_id)
        );
CREATE TABLE snapshot_deployments (
            deployment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            deployed_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(snapshot_id) REFERENCES catalog_snapshots(snapshot_id)
        );
CREATE TABLE reference_images (
            image_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            current_version_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE INDEX ix_reference_images_type
            ON reference_images(media_type,status,name);
CREATE TABLE reference_image_versions (
            version_id TEXT PRIMARY KEY,
            image_id TEXT NOT NULL,
            version TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            application_version TEXT,
            checksum TEXT,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            catalog_items INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            published_at TEXT,
            notes TEXT,
            FOREIGN KEY(image_id) REFERENCES reference_images(image_id) ON DELETE CASCADE,
            FOREIGN KEY(snapshot_id) REFERENCES catalog_snapshots(snapshot_id)
        );
CREATE UNIQUE INDEX ux_reference_image_version
            ON reference_image_versions(image_id,version);
CREATE TABLE node_reference_cache (
            node_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            local_path TEXT,
            checksum TEXT,
            status TEXT NOT NULL DEFAULT 'missing',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(node_id,version_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(version_id) REFERENCES reference_image_versions(version_id)
        );
CREATE TABLE node_tags (
            tag_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            system_tag INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE node_tag_assignments (
            node_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            PRIMARY KEY(node_id,tag_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES node_tags(tag_id) ON DELETE CASCADE
        );
CREATE TABLE placement_settings (
            setting_id TEXT PRIMARY KEY,
            default_mode TEXT NOT NULL DEFAULT 'manual',
            automatic_required_tag TEXT NOT NULL DEFAULT 'appbox-node',
            automatic_excluded_tag TEXT NOT NULL DEFAULT 'bare-metal',
            allow_manual_bare_metal INTEGER NOT NULL DEFAULT 1,
            require_confirmation_bare_metal INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );
CREATE TABLE placement_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            requested_mode TEXT NOT NULL,
            requested_node_id TEXT,
            selected_node_id TEXT,
            eligible_nodes_json TEXT NOT NULL DEFAULT '[]',
            rejected_nodes_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(selected_node_id) REFERENCES nodes(node_id)
        );
CREATE TABLE node_agents (
            node_id TEXT PRIMARY KEY,
            agent_id TEXT,
            agent_version TEXT,
            status TEXT NOT NULL DEFAULT 'not_installed',
            endpoint TEXT,
            token_fingerprint TEXT,
            last_heartbeat TEXT,
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            registered_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
CREATE TABLE reference_image_distribution (
            distribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'missing',
            local_path TEXT,
            expected_checksum TEXT,
            actual_checksum TEXT,
            bytes_total INTEGER NOT NULL DEFAULT 0,
            bytes_transferred INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(version_id,node_id),
            FOREIGN KEY(version_id) REFERENCES reference_image_versions(version_id) ON DELETE CASCADE,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
CREATE TABLE control_plane_deployments (
            deployment_id TEXT PRIMARY KEY,
            client_id TEXT,
            node_id TEXT,
            placement_decision_id INTEGER,
            reference_version_id TEXT,
            status TEXT NOT NULL DEFAULT 'planned',
            current_step TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(placement_decision_id) REFERENCES placement_decisions(decision_id),
            FOREIGN KEY(reference_version_id) REFERENCES reference_image_versions(version_id)
        );
CREATE TABLE agent_enrollment_tokens (
            token_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            label TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            used_at TEXT,
            revoked_at TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
CREATE TABLE agent_commands (
            command_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            command_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            result_json TEXT,
            error_text TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
CREATE INDEX ix_agent_commands_node_status
            ON agent_commands(node_id,status,created_at);
CREATE TABLE agent_node_metrics (
            node_id TEXT PRIMARY KEY,
            hostname TEXT,
            os_name TEXT,
            kernel_version TEXT,
            docker_version TEXT,
            compose_version TEXT,
            cpu_model TEXT,
            cpu_count INTEGER,
            load_1 REAL,
            memory_total_bytes INTEGER,
            memory_available_bytes INTEGER,
            disk_total_bytes INTEGER,
            disk_free_bytes INTEGER,
            temperature_c REAL,
            gpu_present INTEGER NOT NULL DEFAULT 0,
            rdad_present INTEGER NOT NULL DEFAULT 0,
            docker_ok INTEGER NOT NULL DEFAULT 0,
            collected_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
CREATE TABLE reconciliation_events (
            reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            node_id TEXT NOT NULL,
            desired_state TEXT NOT NULL,
            observed_state TEXT NOT NULL,
            result TEXT NOT NULL,
            drift_json TEXT NOT NULL DEFAULT '[]',
            message TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id) ON DELETE CASCADE,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
CREATE INDEX ix_reconciliation_client_time
            ON reconciliation_events(client_id, created_at DESC);
CREATE TABLE audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL DEFAULT 'admin',
            action TEXT NOT NULL,
            client_id TEXT,
            node_id TEXT,
            mode TEXT,
            result TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );
CREATE INDEX ix_audit_client_time
            ON audit_log(client_id, created_at DESC);
