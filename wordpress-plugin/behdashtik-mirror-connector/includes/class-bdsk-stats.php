<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Stats {

	private const TOTALS_OPTION  = 'bdsk_stats_totals';
	private const CLEANUP_OPTION = 'bdsk_cleanup_status';

	// ---------------------------------------------------------------------------
	// Request stats — bdsk_stats_totals (autoload=false)
	// ---------------------------------------------------------------------------

	public static function default_totals(): array {
		return [
			'total'    => 0,
			'accepted' => 0,
			'rejected' => 0,
			'by_reason' => [
				'bad_key'              => 0,
				'ip_mismatch'          => 0,
				'connector_disabled'   => 0,
				'read_access_disabled' => 0,
				'feature_disabled'     => 0,
				'rate_limited'         => 0,
			],
			'by_endpoint' => [
				'health'             => 0,
				'db_export_start'    => 0,
				'db_export_status'   => 0,
				'db_export_download' => 0,
				'db_export_confirm'  => 0,
				'media_manifest'     => 0,
				'events_pending'     => 0,
				'events_ack'         => 0,
				'snapshot_order'     => 0,
				'snapshot_product'   => 0,
			],
			'last_successful_at' => null,
			'last_failed_at'     => null,
		];
	}

	public static function get_totals(): array {
		$stored = get_option( self::TOTALS_OPTION, [] );
		if ( ! is_array( $stored ) ) {
			$stored = [];
		}
		$defaults = self::default_totals();
		$result   = array_merge( $defaults, $stored );
		// Deep-merge nested sub-arrays so new reason/endpoint keys don't go missing
		$result['by_reason']   = array_merge( $defaults['by_reason'],   (array) ( $stored['by_reason'] ?? [] ) );
		$result['by_endpoint'] = array_merge( $defaults['by_endpoint'], (array) ( $stored['by_endpoint'] ?? [] ) );
		return $result;
	}

	public static function increment( string $route, string $status, ?string $reason = null ): void {
		$totals = self::get_totals();
		$totals['total']++;
		$now = current_time( 'mysql', true );

		if ( 'accepted' === $status ) {
			$totals['accepted']++;
			$totals['last_successful_at'] = $now;
		} else {
			$totals['rejected']++;
			$totals['last_failed_at'] = $now;
			if ( $reason && array_key_exists( $reason, $totals['by_reason'] ) ) {
				$totals['by_reason'][ $reason ]++;
			}
		}

		$ep = self::route_to_endpoint( $route );
		if ( null !== $ep ) {
			$totals['by_endpoint'][ $ep ]++;
		}

		update_option( self::TOTALS_OPTION, $totals, false );
	}

	public static function reset_totals(): void {
		update_option( self::TOTALS_OPTION, self::default_totals(), false );
	}

	private static function route_to_endpoint( string $route ): ?string {
		if ( str_contains( $route, '/health' ) )               return 'health';
		if ( str_contains( $route, '/db-export/start' ) )      return 'db_export_start';
		if ( str_contains( $route, '/db-export/status' ) )     return 'db_export_status';
		if ( str_contains( $route, '/db-export/download' ) )   return 'db_export_download';
		if ( str_contains( $route, '/db-export/confirm' ) )    return 'db_export_confirm';
		if ( str_contains( $route, '/media-manifest' ) )       return 'media_manifest';
		if ( str_contains( $route, '/events/pending' ) )       return 'events_pending';
		if ( str_contains( $route, '/events/ack' ) )           return 'events_ack';
		if ( str_contains( $route, '/snapshot/order' ) )       return 'snapshot_order';
		if ( str_contains( $route, '/snapshot/product' ) )     return 'snapshot_product';
		return null;
	}

	// ---------------------------------------------------------------------------
	// Cleanup status — bdsk_cleanup_status (autoload=false)
	// ---------------------------------------------------------------------------

	public static function get_cleanup_status(): array {
		$val = get_option( self::CLEANUP_OPTION, [] );
		return is_array( $val ) ? $val : [];
	}

	public static function save_cleanup_status( array $summary ): void {
		update_option( self::CLEANUP_OPTION, [
			'last_run_at'      => gmdate( 'c' ),
			'last_run_summary' => $summary,
		], false );
	}

	// ---------------------------------------------------------------------------
	// Dashboard data aggregation
	// ---------------------------------------------------------------------------

	public static function get_dashboard_data(): array {
		global $wpdb;

		$totals  = self::get_totals();
		$cleanup = self::get_cleanup_status();

		// Connection status: "connected" if last successful request was within 10 min
		$last_ok     = $totals['last_successful_at'];
		$conn_status = 'never';
		if ( $last_ok ) {
			$conn_status = ( time() - (int) strtotime( $last_ok ) ) <= 600 ? 'connected' : 'stale';
		}

		// Export stats
		$jobs_table = BDSK_DB::jobs_table();
		$export_agg = $wpdb->get_row(
			"SELECT COUNT(*) AS cnt, MAX(created_at) AS last_created FROM {$jobs_table}",
			ARRAY_A
		);
		$last_dl    = $wpdb->get_var(
			"SELECT MAX(finished_at) FROM {$jobs_table} WHERE status IN ('downloaded','cleaned')"
		);
		$last_job   = $wpdb->get_row(
			"SELECT status, last_error FROM {$jobs_table} ORDER BY id DESC LIMIT 1",
			ARRAY_A
		);

		// Media stats
		$media_status = BDSK_Media_Index::get_status();
		$media_counts = $wpdb->get_results(
			"SELECT image_type, status, COUNT(*) AS cnt FROM " . BDSK_DB::media_index_table() .
			" GROUP BY image_type, status ORDER BY image_type, status",
			ARRAY_A
		) ?: [];

		return [
			'connector' => [
				'connector_enabled'  => (bool) BDSK_Settings::get( 'enabled' ),
				'read_access'        => (bool) BDSK_Settings::get( 'read_access_enabled' ),
				'write_access'       => false,
				'plugin_version'     => BDSK_VERSION,
				'wp_version'         => get_bloginfo( 'version' ),
				'php_version'        => PHP_VERSION,
				'wc_version'         => defined( 'WC_VERSION' ) ? WC_VERSION : null,
				'last_connection'    => [
					'status'             => $conn_status,
					'last_successful_at' => $last_ok,
				],
			],
			'requests' => $totals,
			'exports'  => [
				'total_jobs'         => (int) ( $export_agg['cnt'] ?? 0 ),
				'last_created_at'    => $export_agg['last_created'] ?? null,
				'last_downloaded_at' => $last_dl,
				'last_status'        => $last_job['status'] ?? null,
				'last_error'         => ( $last_job && 'failed' === $last_job['status'] )
					? $last_job['last_error'] : null,
			],
			'media' => [
				'index_status'       => $media_status['status'],
				'last_full_build_at' => $media_status['last_full_build_at'],
				'counts'             => $media_counts,
			],
			'events'  => BDSK_Event_Outbox::get_stats(),
			'cleanup' => $cleanup,
		];
	}
}
