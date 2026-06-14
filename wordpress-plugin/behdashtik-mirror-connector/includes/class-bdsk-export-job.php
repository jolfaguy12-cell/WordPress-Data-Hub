<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Export_Job {

	private const AS_CHUNK_HOOK    = 'bdsk_export_chunk';
	private const AS_HEARTBEAT_HOOK = 'bdsk_check_stuck_jobs';

	// ---------------------------------------------------------------------------
	// Action Scheduler registration
	// ---------------------------------------------------------------------------

	public static function init_scheduler(): void {
		add_action( self::AS_CHUNK_HOOK,    [ __CLASS__, 'process_chunk' ] );
		add_action( self::AS_HEARTBEAT_HOOK, [ __CLASS__, 'check_stuck_jobs' ] );
	}

	public static function schedule_heartbeat_check(): void {
		if ( ! function_exists( 'as_has_scheduled_action' ) ) {
			return;
		}
		if ( ! as_has_scheduled_action( self::AS_HEARTBEAT_HOOK ) ) {
			as_schedule_recurring_action( time(), 300, self::AS_HEARTBEAT_HOOK, [], 'bdsk' );
		}
	}

	// ---------------------------------------------------------------------------
	// Job creation
	// ---------------------------------------------------------------------------

	/**
	 * Creates a new export job and schedules the first chunk.
	 * Returns [ 'job_id' => ..., 'status' => 'pending' ] or WP_Error.
	 */
	public static function create( bool $test_mode = false ): array|WP_Error {
		if ( ! function_exists( 'as_enqueue_async_action' ) ) {
			return new WP_Error( 'action_scheduler_missing', 'Action Scheduler is not available. Ensure WooCommerce is active.', [ 'status' => 500 ] );
		}

		// Concurrency lock: only one active job at a time
		$active = BDSK_DB::get_active_job();
		if ( $active ) {
			return new WP_Error(
				'export_already_running',
				'An export job is already running.',
				[ 'status' => 409, 'job_id' => $active['job_id'] ]
			);
		}

		if ( ! BDSK_Settings::get( 'backup_export_enabled' ) ) {
			return new WP_Error( 'backup_export_disabled', 'Backup export is not enabled.', [ 'status' => 403 ] );
		}

		$job_id = BDSK_DB::create_job( [
			'status'     => 'pending',
			'started_at' => current_time( 'mysql', true ),
		] );

		if ( ! $job_id ) {
			return new WP_Error( 'db_error', 'Could not create export job.', [ 'status' => 500 ] );
		}

		// Store test mode flag in a short-lived transient (15 minutes)
		if ( $test_mode ) {
			set_transient( 'bdsk_test_mode_' . $job_id, true, 900 );
		}

		// Schedule the initialisation chunk immediately
		as_enqueue_async_action( self::AS_CHUNK_HOOK, [ $job_id ], 'bdsk' );

		bdsk_log( "Export job created: {$job_id}", [ 'test_mode' => $test_mode ] );

		return [ 'job_id' => $job_id, 'status' => 'pending' ];
	}

	// ---------------------------------------------------------------------------
	// Chunk processor — called by Action Scheduler
	// ---------------------------------------------------------------------------

	public static function process_chunk( string $job_id ): void {
		global $wpdb;

		$job = BDSK_DB::get_job( $job_id );
		if ( ! $job || ! in_array( $job['status'], [ 'pending', 'running' ], true ) ) {
			return;
		}

		// Update heartbeat immediately
		BDSK_DB::update_job( $job_id, [
			'status'       => 'running',
			'heartbeat_at' => current_time( 'mysql', true ),
		] );

		try {
			$manifest = json_decode( $job['archive_manifest'] ?: '{}', true ) ?: [];

			// ---------------------------------------------------------------
			// First chunk: initialise table list
			// ---------------------------------------------------------------
			if ( empty( $manifest ) ) {
				$tables = self::get_tables_list();
				$manifest = [
					'db_prefix'        => $wpdb->prefix,
					'tables_to_export' => $tables,
					'tables_included'  => [],
					'current_part'     => 1,
					'parts'            => [],
				];
				BDSK_DB::update_job( $job_id, [
					'total_tables'    => count( $tables ),
					'current_table'   => $tables[0] ?? null,
					'current_offset'  => 0,
					'archive_manifest' => wp_json_encode( $manifest ),
					'heartbeat_at'    => current_time( 'mysql', true ),
				] );
				self::ensure_export_dir( $job_id );
				// Write SQL header to part 1 file
				self::write_sql_file_header( $job_id, 1 );
				as_enqueue_async_action( self::AS_CHUNK_HOOK, [ $job_id ], 'bdsk' );
				return;
			}

			// ---------------------------------------------------------------
			// Subsequent chunks: export rows
			// ---------------------------------------------------------------
			$current_table  = $job['current_table'];
			$current_offset = (int) $job['current_offset'];
			$part_num       = (int) ( $manifest['current_part'] ?? 1 );
			$part_path      = self::get_part_path( $job_id, $part_num );
			$test_mode      = (bool) get_transient( 'bdsk_test_mode_' . $job_id );
			$chunk_size     = (int) apply_filters( 'bdsk_export_chunk_size', BDSK_EXPORT_CHUNK_SIZE );

			if ( $test_mode ) {
				$chunk_size = min( $chunk_size, BDSK_TEST_EXPORT_ROWS );
			}

			// Write table schema when starting a new table
			if ( 0 === $current_offset ) {
				$schema_sql = self::get_table_schema_sql( $current_table );
				if ( '' !== $schema_sql ) {
					self::gz_append( $part_path, $schema_sql );
				}
			}

			// Export this batch of rows
			$rows_sql   = self::get_rows_sql( $current_table, $current_offset, $chunk_size );
			$rows_count = $rows_sql['count'];

			if ( '' !== $rows_sql['sql'] ) {
				self::gz_append( $part_path, $rows_sql['sql'] );
			}

			// Advance test-mode limit
			$effective_limit = $test_mode ? BDSK_TEST_EXPORT_ROWS : PHP_INT_MAX;
			$new_offset      = $current_offset + $rows_count;
			$table_done      = $rows_count < $chunk_size || $new_offset >= $effective_limit;

			$job_update = [
				'current_offset'      => $new_offset,
				'exported_rows_count' => ( (int) $job['exported_rows_count'] ) + $rows_count,
				'heartbeat_at'        => current_time( 'mysql', true ),
			];

			if ( $table_done ) {
				$manifest['tables_included'][] = $current_table;
				$remaining = array_values(
					array_diff( $manifest['tables_to_export'], $manifest['tables_included'] )
				);
				$manifest['tables_to_export'] = $remaining;
				$next_table                   = $remaining[0] ?? null;

				$tables_done = (int) $job['tables_completed'] + 1;
				$job_update['tables_completed'] = $tables_done;
				$job_update['current_table']    = $next_table;
				$job_update['current_offset']   = 0;
				$job_update['progress_percent'] = round(
					( $tables_done / max( (int) $job['total_tables'], 1 ) ) * 100,
					1
				);

				// Roll over to a new part if compressed file exceeds threshold
				clearstatcache( true, $part_path );
				$part_bytes     = file_exists( $part_path ) ? filesize( $part_path ) : 0;
				$part_threshold = (int) apply_filters( 'bdsk_export_part_size', BDSK_EXPORT_PART_SIZE );

				if ( $next_table !== null && $part_bytes >= $part_threshold ) {
					$part_info             = self::finalise_part( $job_id, $part_num );
					$manifest['parts'][]   = $part_info;
					$part_num++;
					$manifest['current_part'] = $part_num;
					// Write header to new part file
					self::write_sql_file_header( $job_id, $part_num );
				}

				if ( null === $next_table ) {
					// All tables exported — finalise
					$part_info           = self::finalise_part( $job_id, $part_num );
					$manifest['parts'][] = $part_info;

					$final_manifest = [
						'parts'            => $manifest['parts'],
						'tables_included'  => $manifest['tables_included'],
						'db_prefix'        => $manifest['db_prefix'],
					];
					$manifest_json     = wp_json_encode( $final_manifest );
					$manifest_checksum = hash( 'sha256', $manifest_json );

					$total_size = array_sum( array_column( $manifest['parts'], 'size' ) );

					$job_update['status']           = 'ready';
					$job_update['finished_at']      = current_time( 'mysql', true );
					$job_update['progress_percent'] = 100.0;
					$job_update['archive_manifest'] = $manifest_json;
					$job_update['checksum']         = $manifest_checksum;
					$job_update['archive_size']     = $total_size;

					BDSK_DB::update_job( $job_id, $job_update );

					// Generate download token AFTER updating job status
					self::generate_download_token( $job_id );

					bdsk_log( "Export job {$job_id} finished. Parts: " . count( $manifest['parts'] ) );
					return;
				}
			} else {
				// Still inside the same table — calculate progress
				$total_in_table = (int) $wpdb->get_var(
					'SELECT COUNT(*) FROM `' . esc_sql( $current_table ) . '`'
				);
				if ( $total_in_table > 0 && (int) $job['total_tables'] > 0 ) {
					$table_progress = min( $new_offset / $total_in_table, 1.0 );
					$base           = ( (int) $job['tables_completed'] / (int) $job['total_tables'] ) * 100;
					$per_table      = ( 1 / (int) $job['total_tables'] ) * 100;
					$job_update['progress_percent'] = round( $base + $table_progress * $per_table, 1 );
				}
			}

			$job_update['archive_manifest'] = wp_json_encode( $manifest );
			BDSK_DB::update_job( $job_id, $job_update );

			as_enqueue_async_action( self::AS_CHUNK_HOOK, [ $job_id ], 'bdsk' );

		} catch ( \Throwable $e ) {
			$retry = (int) $job['retry_count'] + 1;
			if ( $retry >= BDSK_MAX_RETRY_COUNT ) {
				BDSK_DB::update_job( $job_id, [
					'status'     => 'failed',
					'last_error' => substr( $e->getMessage(), 0, 500 ),
				] );
				bdsk_log( "Export job {$job_id} failed permanently: " . $e->getMessage() );
			} else {
				BDSK_DB::update_job( $job_id, [
					'retry_count' => $retry,
					'last_error'  => substr( $e->getMessage(), 0, 500 ),
					'heartbeat_at' => current_time( 'mysql', true ),
				] );
				bdsk_log( "Export job {$job_id} chunk error (retry {$retry}): " . $e->getMessage() );
				as_schedule_single_action( time() + 30, self::AS_CHUNK_HOOK, [ $job_id ], 'bdsk' );
			}
		}
	}

	// ---------------------------------------------------------------------------
	// Stuck-job heartbeat checker
	// ---------------------------------------------------------------------------

	public static function check_stuck_jobs(): void {
		$stalled = BDSK_DB::get_stalled_jobs( BDSK_HEARTBEAT_TIMEOUT );
		foreach ( $stalled as $job ) {
			BDSK_DB::update_job( $job['job_id'], [
				'status'     => 'failed',
				'last_error' => 'stalled - no heartbeat',
			] );
			bdsk_log( "Marked job {$job['job_id']} as failed (stalled)." );
		}
	}

	// ---------------------------------------------------------------------------
	// Download token
	// ---------------------------------------------------------------------------

	public static function generate_download_token( string $job_id ): string {
		$secret  = BDSK_Security::get_api_secret();
		$expires = time() + BDSK_DOWNLOAD_TOKEN_TTL;

		$payload   = $job_id . '.' . $expires;
		$signature = hash_hmac( 'sha256', $payload, $secret );
		$raw_token = $payload . '.' . $signature;
		$token     = bdsk_base64url_encode( $raw_token );

		BDSK_DB::update_job( $job_id, [
			'download_token_hash'       => hash( 'sha256', $token ),
			'download_token_expires_at' => gmdate( 'Y-m-d H:i:s', $expires ),
		] );

		return $token;
	}

	public static function validate_download_token( string $job_id, string $token ): bool {
		if ( '' === $token || '' === $job_id ) {
			return false;
		}

		$job = BDSK_DB::get_job( $job_id );
		if ( ! $job ) {
			return false;
		}

		// Reject if token has already been invalidated
		if ( empty( $job['download_token_hash'] ) ) {
			return false;
		}

		// Constant-time hash comparison
		if ( ! hash_equals( $job['download_token_hash'], hash( 'sha256', $token ) ) ) {
			return false;
		}

		// Decode and verify payload
		$raw_token = bdsk_base64url_decode( $token );
		$parts     = explode( '.', $raw_token, 3 );
		if ( count( $parts ) !== 3 ) {
			return false;
		}

		[ $token_job_id, $expires, $signature ] = $parts;

		if ( $token_job_id !== $job_id ) {
			return false;
		}

		if ( (int) $expires < time() ) {
			return false; // expired
		}

		$secret           = BDSK_Security::get_api_secret();
		$expected_sig     = hash_hmac( 'sha256', $token_job_id . '.' . $expires, $secret );
		if ( ! hash_equals( $expected_sig, $signature ) ) {
			return false;
		}

		if ( ! in_array( $job['status'], [ 'ready', 'downloading' ], true ) ) {
			return false;
		}

		return true;
	}

	// ---------------------------------------------------------------------------
	// Export directory helpers
	// ---------------------------------------------------------------------------

	public static function get_export_base(): string {
		$upload = wp_upload_dir();
		return $upload['basedir'] . '/bdsk-exports';
	}

	public static function get_export_dir( string $job_id ): string {
		return self::get_export_base() . '/' . $job_id;
	}

	public static function get_part_path( string $job_id, int $part_num ): string {
		return self::get_export_dir( $job_id ) . "/export_{$job_id}_part{$part_num}.sql.gz";
	}

	public static function ensure_export_dir( string $job_id ): void {
		$base = self::get_export_base();
		$dir  = self::get_export_dir( $job_id );

		wp_mkdir_p( $base );
		wp_mkdir_p( $dir );

		// Protect base directory from direct web access
		$htaccess = $base . '/.htaccess';
		if ( ! file_exists( $htaccess ) ) {
			// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
			file_put_contents( $htaccess, "Deny from all\n" );
		}
		$index = $base . '/index.php';
		if ( ! file_exists( $index ) ) {
			// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
			file_put_contents( $index, '<?php // Silence is golden.' );
		}
	}

	// ---------------------------------------------------------------------------
	// SQL generation helpers
	// ---------------------------------------------------------------------------

	private static function get_tables_list(): array {
		global $wpdb;
		$like   = $wpdb->esc_like( $wpdb->prefix ) . '%';
		$tables = $wpdb->get_col( $wpdb->prepare( 'SHOW TABLES LIKE %s', $like ) );
		return $tables ?: [];
	}

	private static function get_table_schema_sql( string $table ): string {
		global $wpdb;
		$row = $wpdb->get_row(
			'SHOW CREATE TABLE `' . esc_sql( $table ) . '`',
			ARRAY_N
		);
		if ( ! $row ) {
			return '';
		}
		$sql  = "\n-- ---------------------------------------------------\n";
		$sql .= "-- Table: `{$table}`\n";
		$sql .= "-- ---------------------------------------------------\n\n";
		$sql .= 'DROP TABLE IF EXISTS `' . $table . '`;' . "\n";
		$sql .= $row[1] . ";\n\n";
		return $sql;
	}

	private static function get_rows_sql( string $table, int $offset, int $limit ): array {
		global $wpdb;

		$columns = $wpdb->get_col(
			'SHOW COLUMNS FROM `' . esc_sql( $table ) . '`',
			0
		);
		if ( empty( $columns ) ) {
			return [ 'sql' => '', 'count' => 0 ];
		}

		$rows = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT * FROM `' . esc_sql( $table ) . '` LIMIT %d OFFSET %d',
				$limit,
				$offset
			),
			ARRAY_N
		);

		if ( empty( $rows ) ) {
			return [ 'sql' => '', 'count' => 0 ];
		}

		$col_list      = implode( ', ', array_map( fn( $c ) => '`' . $c . '`', $columns ) );
		$insert_prefix = "INSERT INTO `{$table}` ({$col_list}) VALUES\n";
		$sql           = '';
		$batch_size    = 100;
		$value_rows    = [];

		foreach ( $rows as $row ) {
			$values       = array_map( [ __CLASS__, 'format_sql_value' ], (array) $row );
			$value_rows[] = '(' . implode( ', ', $values ) . ')';
		}

		for ( $i = 0, $total = count( $value_rows ); $i < $total; $i += $batch_size ) {
			$batch = array_slice( $value_rows, $i, $batch_size );
			$sql  .= $insert_prefix . implode( ",\n", $batch ) . ";\n";
		}

		return [ 'sql' => $sql, 'count' => count( $rows ) ];
	}

	private static function format_sql_value( mixed $value ): string {
		if ( null === $value ) {
			return 'NULL';
		}
		global $wpdb;
		// WordPress 7.0 changed _real_escape() to replace '%' with an internal
		// hash placeholder (to protect LIKE wildcards), which corrupts SQL values
		// that legitimately contain '%' (e.g. URL-encoded slugs).
		// mysqli_real_escape_string() escapes only actual MySQL string-literal
		// special chars (\0 \n \r \ ' " \Z) and never touches '%'.
		if ( ! ( $wpdb->dbh instanceof mysqli ) ) {
			throw new \RuntimeException( 'BDSK requires a MySQLi database driver.' );
		}
		return "'" . mysqli_real_escape_string( $wpdb->dbh, (string) $value ) . "'";
	}

	// ---------------------------------------------------------------------------
	// Gzip file helpers
	// ---------------------------------------------------------------------------

	/**
	 * Write SQL preamble (charset, FK checks) at the start of a new part file.
	 */
	private static function write_sql_file_header( string $job_id, int $part_num ): void {
		$path   = self::get_part_path( $job_id, $part_num );
		$header = "-- Behdashtik Mirror Connector export\n";
		$header .= "-- Job: {$job_id}  Part: {$part_num}\n";
		$header .= "-- Generated: " . gmdate( 'c' ) . "\n\n";
		$header .= "SET NAMES utf8mb4;\n";
		$header .= "SET FOREIGN_KEY_CHECKS=0;\n";
		$header .= "SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n\n";
		self::gz_append( $path, $header );
	}

	/**
	 * Appends $data to a gzip file, creating a new gzip member each call.
	 * Concatenated gzip streams are valid and decompress correctly with gunzip/zcat.
	 */
	private static function gz_append( string $path, string $data ): void {
		// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
		$gz = gzopen( $path, 'ab9' );
		if ( ! $gz ) {
			throw new \RuntimeException( "Cannot open gzip file for writing: {$path}" );
		}
		gzwrite( $gz, $data );
		gzclose( $gz );
	}

	/**
	 * Finalises a part: write footer SQL, compute checksum, return manifest entry.
	 */
	private static function finalise_part( string $job_id, int $part_num ): array {
		$path   = self::get_part_path( $job_id, $part_num );
		$footer = "\nSET FOREIGN_KEY_CHECKS=1;\n";
		self::gz_append( $path, $footer );

		clearstatcache( true, $path );
		$size     = file_exists( $path ) ? filesize( $path ) : 0;
		$checksum = hash_file( 'sha256', $path );

		return [
			'filename' => basename( $path ),
			'size'     => $size,
			'sha256'   => $checksum,
		];
	}
}
