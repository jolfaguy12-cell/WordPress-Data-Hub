<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_DB {

	// ---------------------------------------------------------------------------
	// Table name helpers
	// ---------------------------------------------------------------------------

	public static function jobs_table(): string {
		global $wpdb;
		return $wpdb->prefix . 'bdsk_export_jobs';
	}

	public static function log_table(): string {
		global $wpdb;
		return $wpdb->prefix . 'bdsk_request_log';
	}

	// ---------------------------------------------------------------------------
	// Activation: create custom tables
	// ---------------------------------------------------------------------------

	public static function create_tables(): void {
		global $wpdb;
		require_once ABSPATH . 'wp-admin/includes/upgrade.php';

		$charset = $wpdb->get_charset_collate();

		$jobs_sql = "CREATE TABLE " . self::jobs_table() . " (
			id                       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
			job_id                   VARCHAR(36)     NOT NULL,
			status                   VARCHAR(20)     NOT NULL DEFAULT 'pending',
			created_at               DATETIME        NOT NULL,
			updated_at               DATETIME        NOT NULL,
			heartbeat_at             DATETIME                 DEFAULT NULL,
			started_at               DATETIME                 DEFAULT NULL,
			finished_at              DATETIME                 DEFAULT NULL,
			current_table            VARCHAR(191)             DEFAULT NULL,
			current_offset           BIGINT UNSIGNED NOT NULL DEFAULT 0,
			total_tables             INT             NOT NULL DEFAULT 0,
			tables_completed         INT             NOT NULL DEFAULT 0,
			exported_rows_count      BIGINT UNSIGNED NOT NULL DEFAULT 0,
			progress_percent         FLOAT           NOT NULL DEFAULT 0,
			archive_manifest         LONGTEXT                 DEFAULT NULL,
			archive_size             BIGINT UNSIGNED          DEFAULT NULL,
			checksum                 VARCHAR(64)              DEFAULT NULL,
			download_token_hash      VARCHAR(64)              DEFAULT NULL,
			download_token_expires_at DATETIME                DEFAULT NULL,
			last_error               TEXT                     DEFAULT NULL,
			cleanup_status           VARCHAR(20)     NOT NULL DEFAULT 'pending',
			retry_count              INT             NOT NULL DEFAULT 0,
			PRIMARY KEY  (id),
			UNIQUE KEY job_id (job_id),
			KEY status (status),
			KEY cleanup_status (cleanup_status)
		) $charset;";

		$log_sql = "CREATE TABLE " . self::log_table() . " (
			id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
			created_at  DATETIME        NOT NULL,
			endpoint    VARCHAR(191)    NOT NULL DEFAULT '',
			ip          VARCHAR(45)     NOT NULL DEFAULT '',
			status      VARCHAR(20)     NOT NULL DEFAULT '',
			reason      VARCHAR(100)             DEFAULT NULL,
			duration_ms INT                      DEFAULT NULL,
			PRIMARY KEY (id),
			KEY created_at (created_at),
			KEY status (status)
		) $charset;";

		dbDelta( $jobs_sql );
		dbDelta( $log_sql );
	}

	// ---------------------------------------------------------------------------
	// Job CRUD
	// ---------------------------------------------------------------------------

	public static function create_job( array $data ): string|false {
		global $wpdb;

		$job_id = wp_generate_uuid4();
		$now    = current_time( 'mysql', true );

		$row = array_merge( [
			'job_id'     => $job_id,
			'status'     => 'pending',
			'created_at' => $now,
			'updated_at' => $now,
		], $data );

		$result = $wpdb->insert( self::jobs_table(), $row );
		return false === $result ? false : $job_id;
	}

	public static function get_job( string $job_id ): array|false {
		global $wpdb;

		$row = $wpdb->get_row(
			$wpdb->prepare( 'SELECT * FROM ' . self::jobs_table() . ' WHERE job_id = %s LIMIT 1', $job_id ),
			ARRAY_A
		);

		return $row ?: false;
	}

	public static function update_job( string $job_id, array $data ): bool {
		global $wpdb;

		$data['updated_at'] = current_time( 'mysql', true );

		$result = $wpdb->update(
			self::jobs_table(),
			$data,
			[ 'job_id' => $job_id ]
		);

		return false !== $result;
	}

	/**
	 * Returns any job currently in pending or running state (concurrency check).
	 */
	public static function get_active_job(): array|false {
		global $wpdb;

		$row = $wpdb->get_row(
			"SELECT * FROM " . self::jobs_table() . "
			 WHERE status IN ('pending','running')
			 ORDER BY created_at ASC LIMIT 1",
			ARRAY_A
		);

		return $row ?: false;
	}

	/**
	 * Returns jobs needing heartbeat check (running but stalled).
	 */
	public static function get_stalled_jobs( int $timeout_seconds ): array {
		global $wpdb;

		$cutoff = gmdate( 'Y-m-d H:i:s', time() - $timeout_seconds );

		return $wpdb->get_results(
			$wpdb->prepare(
				"SELECT * FROM " . self::jobs_table() . "
				 WHERE status = 'running'
				 AND heartbeat_at < %s",
				$cutoff
			),
			ARRAY_A
		) ?: [];
	}

	/**
	 * Returns jobs eligible for file cleanup.
	 */
	public static function get_jobs_for_cleanup(): array {
		global $wpdb;

		$now = current_time( 'mysql', true );

		return $wpdb->get_results(
			$wpdb->prepare(
				"SELECT * FROM " . self::jobs_table() . "
				 WHERE cleanup_status = 'pending'
				 AND status IN ('downloaded','failed','expired')
				 OR (
				   cleanup_status = 'pending'
				   AND status IN ('ready','downloading')
				   AND download_token_expires_at < DATE_SUB(%s, INTERVAL 1 HOUR)
				 )",
				$now
			),
			ARRAY_A
		) ?: [];
	}

	// ---------------------------------------------------------------------------
	// Request log
	// ---------------------------------------------------------------------------

	public static function log_request(
		string  $endpoint,
		string  $ip,
		string  $status,
		?string $reason      = null,
		?int    $duration_ms = null
	): void {
		global $wpdb;

		$wpdb->insert( self::log_table(), [
			'created_at'  => current_time( 'mysql', true ),
			'endpoint'    => substr( $endpoint, 0, 191 ),
			'ip'          => substr( $ip, 0, 45 ),
			'status'      => $status,
			'reason'      => $reason,
			'duration_ms' => $duration_ms,
		] );
	}
}
