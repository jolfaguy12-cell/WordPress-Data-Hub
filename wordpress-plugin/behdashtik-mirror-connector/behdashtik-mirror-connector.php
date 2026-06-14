<?php
/**
 * Plugin Name:       Behdashtik Mirror Connector
 * Plugin URI:        https://github.com/jolfaguy12-cell/WordPress-Data-Hub
 * Description:       Secure database export pipeline for the Behdashtik WordPress mirror system.
 * Version:           1.0.0
 * Requires at least: 6.0
 * Requires PHP:      8.0
 * Author:            Behdashtik
 * License:           GPL-2.0-or-later
 * Text Domain:       bdsk
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

define( 'BDSK_VERSION',            '1.0.0' );
define( 'BDSK_PLUGIN_DIR',         plugin_dir_path( __FILE__ ) );
define( 'BDSK_PLUGIN_URL',         plugin_dir_url( __FILE__ ) );
define( 'BDSK_EXPORT_CHUNK_SIZE',  500 );          // rows per batch (filterable)
define( 'BDSK_EXPORT_PART_SIZE',   524288000 );    // 500 MB compressed threshold per part
define( 'BDSK_HEARTBEAT_TIMEOUT',  900 );          // 15 minutes; stalled job timeout
define( 'BDSK_MAX_RETRY_COUNT',    3 );
define( 'BDSK_DOWNLOAD_TOKEN_TTL', 21600 );        // 6 hours
define( 'BDSK_TEST_EXPORT_ROWS',   50 );

foreach ( [
	'includes/class-bdsk-db.php',
	'includes/class-bdsk-security.php',
	'includes/class-bdsk-health.php',
	'includes/class-bdsk-export-job.php',
	'includes/class-bdsk-export-rest.php',
	'includes/class-bdsk-cleanup.php',
	'admin/class-bdsk-settings-page.php',
] as $file ) {
	require_once BDSK_PLUGIN_DIR . $file;
}

// ---------------------------------------------------------------------------
// Global helpers
// ---------------------------------------------------------------------------

function bdsk_log( string $message, array $context = [] ): void {
	if ( ! BDSK_Settings::get( 'debug_log_enabled' ) ) {
		return;
	}
	$log_file  = WP_CONTENT_DIR . '/bdsk-debug.log';
	$timestamp = gmdate( 'Y-m-d H:i:s' );
	$line      = "[{$timestamp}] {$message}";
	if ( ! empty( $context ) ) {
		$line .= ' ' . wp_json_encode( $context );
	}
	// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
	file_put_contents( $log_file, $line . PHP_EOL, FILE_APPEND | LOCK_EX );
}

function bdsk_base64url_encode( string $data ): string {
	return rtrim( strtr( base64_encode( $data ), '+/', '-_' ), '=' );
}

function bdsk_base64url_decode( string $data ): string {
	return base64_decode( strtr( $data, '-_', '+/' ) );
}

// ---------------------------------------------------------------------------
// Lifecycle hooks
// ---------------------------------------------------------------------------

register_activation_hook( __FILE__, static function () {
	BDSK_DB::create_tables();
	BDSK_Cleanup::schedule_recurring();
	BDSK_Export_Job::schedule_heartbeat_check();
} );

register_deactivation_hook( __FILE__, static function () {
	if ( function_exists( 'as_unschedule_all_actions' ) ) {
		as_unschedule_all_actions( 'bdsk_cleanup_expired' );
		as_unschedule_all_actions( 'bdsk_check_stuck_jobs' );
	}
} );

add_action( 'plugins_loaded', static function () {
	// Ensure recurring schedules survive plugin updates / re-activations.
	if ( function_exists( 'as_next_scheduled_action' ) ) {
		BDSK_Cleanup::schedule_recurring();
		BDSK_Export_Job::schedule_heartbeat_check();
	}

	BDSK_Export_Rest::init();
	BDSK_Cleanup::init();
	BDSK_Export_Job::init_scheduler();

	if ( is_admin() ) {
		BDSK_Settings_Page::init();
	}
} );
