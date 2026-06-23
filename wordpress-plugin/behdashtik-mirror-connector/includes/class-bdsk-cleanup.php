<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Cleanup {

	private const AS_CLEANUP_HOOK = 'bdsk_cleanup_expired';

	public static function init(): void {
		add_action( 'admin_post_bdsk_emergency_cleanup', [ __CLASS__, 'handle_emergency_cleanup' ] );
	}

	public static function schedule_recurring(): void {
		if ( ! function_exists( 'as_has_scheduled_action' ) ) {
			return;
		}
		if ( ! as_has_scheduled_action( self::AS_CLEANUP_HOOK ) ) {
			as_schedule_recurring_action( time(), HOUR_IN_SECONDS, self::AS_CLEANUP_HOOK, [], 'bdsk' );
		}
	}

	// ---------------------------------------------------------------------------
	// Hourly cleanup task
	// ---------------------------------------------------------------------------

	public static function run_cleanup(): void {
		global $wpdb;

		$summary = [
			'export_files_cleaned'    => 0,
			'media_rows_pruned'       => 0,
			'event_rows_pruned'       => 0,
			'request_log_rows_pruned' => 0,
		];

		$jobs = BDSK_DB::get_jobs_for_cleanup();
		foreach ( $jobs as $job ) {
			self::cleanup_job( $job['job_id'] );
			$summary['export_files_cleaned']++;
		}

		$summary['media_rows_pruned'] = BDSK_Media_Index::prune_old_deleted_rows();

		$summary['event_rows_pruned'] = BDSK_Event_Outbox::prune_old_acknowledged();

		// Prune request log rows older than 30 days
		$summary['request_log_rows_pruned'] = (int) $wpdb->query(
			"DELETE FROM " . BDSK_DB::log_table() .
			" WHERE created_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 DAY)"
		);

		BDSK_Stats::save_cleanup_status( $summary );
	}

	/**
	 * Delete archive files for a single job and update DB.
	 */
	public static function cleanup_job( string $job_id ): void {
		$job = BDSK_DB::get_job( $job_id );
		if ( ! $job ) {
			return;
		}

		// In shared_host_no_file_mode no archive files exist; cleanup is a no-op.
		if ( BDSK_Export_Job::get_export_mode() !== 'local_private_archive_mode' ) {
			BDSK_DB::update_job( $job_id, [
				'cleanup_status'   => 'cleaned',
				'archive_manifest' => null,
				'checksum'         => null,
			] );
			return;
		}

		$export_dir  = BDSK_Export_Job::get_export_dir( $job_id );
		$file_errors = [];

		if ( is_dir( $export_dir ) ) {
			$files = glob( $export_dir . '/*' );
			if ( $files ) {
				foreach ( $files as $file ) {
					if ( is_file( $file ) && ! unlink( $file ) ) {
						$file_errors[] = basename( $file );
					}
				}
			}
			if ( ! rmdir( $export_dir ) && is_dir( $export_dir ) ) {
				$file_errors[] = '(directory)';
			}
		}

		$new_status = empty( $file_errors ) ? 'cleaned' : 'cleanup_failed';

		BDSK_DB::update_job( $job_id, [
			'cleanup_status'   => $new_status,
			'archive_manifest' => null,
			'checksum'         => null,
		] );

		if ( $new_status === 'cleanup_failed' ) {
			bdsk_log( "Cleanup failed for job {$job_id}: could not remove: " . implode( ', ', $file_errors ) );
		} else {
			bdsk_log( "Cleaned up export files for job {$job_id}." );
		}
	}

	// ---------------------------------------------------------------------------
	// Emergency cleanup — admin button
	// ---------------------------------------------------------------------------

	public static function handle_emergency_cleanup(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised', 403 );
		}

		check_admin_referer( 'bdsk_emergency_cleanup' );

		global $wpdb;

		// Delete all archive files only in local_private_archive_mode (no files exist in other modes)
		if ( BDSK_Export_Job::get_export_mode() === 'local_private_archive_mode' ) {
			$base = BDSK_Export_Job::get_export_base();
			if ( is_dir( $base ) ) {
				self::rmdir_recursive( $base );
			}
		}

		// Mark all non-cleaned jobs as cleaned
		$wpdb->query(
			"UPDATE " . BDSK_DB::jobs_table() . "
			 SET cleanup_status = 'cleaned', archive_manifest = NULL, checksum = NULL,
			     updated_at = UTC_TIMESTAMP()
			 WHERE cleanup_status != 'cleaned'"
		);

		// Fail any stuck pending/running jobs
		$wpdb->query(
			"UPDATE " . BDSK_DB::jobs_table() . "
			 SET status = 'failed', last_error = 'emergency cleanup',
			     updated_at = UTC_TIMESTAMP()
			 WHERE status IN ('pending','running')"
		);

		bdsk_log( 'Emergency cleanup executed.' );

		wp_safe_redirect( add_query_arg(
			[ 'page' => 'bdsk-settings', 'bdsk_notice' => 'cleanup_done' ],
			admin_url( 'options-general.php' )
		) );
		exit;
	}

	// ---------------------------------------------------------------------------
	// Helper: recursive rmdir
	// ---------------------------------------------------------------------------

	private static function rmdir_recursive( string $dir ): void {
		if ( ! is_dir( $dir ) ) {
			return;
		}
		$items = array_diff( scandir( $dir ), [ '.', '..' ] );
		foreach ( $items as $item ) {
			$path = $dir . '/' . $item;
			if ( is_dir( $path ) ) {
				self::rmdir_recursive( $path );
			} else {
				// phpcs:ignore WordPress.WP.AlternativeFunctions.unlink_unlink
				unlink( $path );
			}
		}
		rmdir( $dir );
	}
}

// Register AS hook handler outside the class init so it fires even if init() hasn't been called yet
add_action( 'bdsk_cleanup_expired', [ 'BDSK_Cleanup', 'run_cleanup' ] );
