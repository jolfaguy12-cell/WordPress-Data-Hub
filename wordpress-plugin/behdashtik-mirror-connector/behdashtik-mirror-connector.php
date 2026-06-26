<?php
/**
 * Plugin Name:       Behdashtik Mirror Connector
 * Plugin URI:        https://github.com/jolfaguy12-cell/WordPress-Data-Hub
 * Description:       Secure database export pipeline for the Behdashtik WordPress mirror system.
 * Version:           1.8.0
 * Requires at least: 7.0
 * Requires PHP:      8.1
 * Author:            Behdashtik
 * License:           GPL-2.0-or-later
 * Text Domain:       bdsk
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

define( 'BDSK_VERSION',            '1.8.0' );
define( 'BDSK_PLUGIN_DIR',         plugin_dir_path( __FILE__ ) );
define( 'BDSK_PLUGIN_URL',         plugin_dir_url( __FILE__ ) );
define( 'BDSK_EXPORT_CHUNK_SIZE',  500 );          // rows per batch — file-based mode (filterable)
define( 'BDSK_STREAMING_CHUNK_SIZE', 50 );        // rows per REST chunk — streaming mode (filterable)
define( 'BDSK_EXPORT_PART_SIZE',   524288000 );    // 500 MB compressed threshold per part
define( 'BDSK_HEARTBEAT_TIMEOUT',  900 );          // 15 minutes; stalled job timeout
define( 'BDSK_MAX_RETRY_COUNT',    3 );
define( 'BDSK_DOWNLOAD_TOKEN_TTL', 21600 );        // 6 hours
define( 'BDSK_TEST_EXPORT_ROWS',   50 );

foreach ( [
	'includes/class-bdsk-db.php',
	'includes/class-bdsk-stats.php',
	'includes/class-bdsk-security.php',
	'includes/class-bdsk-health.php',
	'includes/class-bdsk-export-job.php',
	'includes/class-bdsk-export-rest.php',
	'includes/class-bdsk-cleanup.php',
	'includes/class-bdsk-media-index.php',
	'includes/class-bdsk-media-rest.php',
	'includes/class-bdsk-event-outbox.php',
	'includes/class-bdsk-event-rest.php',
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
	$timestamp = gmdate( 'Y-m-d H:i:s' );
	$line      = "[{$timestamp}] {$message}";
	if ( ! empty( $context ) ) {
		$line .= ' ' . wp_json_encode( $context );
	}
	// Only write to a persistent file in local_private_archive_mode where storage is
	// explicitly configured outside the web root. All other modes route to PHP error_log()
	// to avoid creating unbounded files on the WordPress host.
	if ( BDSK_Export_Job::get_export_mode() === 'local_private_archive_mode' ) {
		// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
		file_put_contents( WP_CONTENT_DIR . '/bdsk-debug.log', $line . PHP_EOL, FILE_APPEND | LOCK_EX );
	} else {
		// phpcs:ignore WordPress.PHP.DevelopmentFunctions.error_log_error_log
		error_log( 'BDSK: ' . $line );
	}
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

	// On first activation with no secret configured: generate one and redirect
	// the admin to the settings page where it will be displayed once.
	if ( extension_loaded( 'openssl' ) && ! BDSK_Security::has_secret() ) {
		$plaintext = BDSK_Security::generate_and_store();
		set_transient( 'bdsk_flash_new_key', $plaintext, 300 );
		set_transient( 'bdsk_activation_redirect', true, 30 );
	}
	// Scheduling is handled by the `init` hook on first load after activation.
} );

register_deactivation_hook( __FILE__, static function () {
	if ( function_exists( 'as_unschedule_all_actions' ) ) {
		as_unschedule_all_actions( 'bdsk_cleanup_expired' );
		as_unschedule_all_actions( 'bdsk_check_stuck_jobs' );
	}
} );

add_action( 'plugins_loaded', static function () {
	// Register REST routes, AS hook handlers, and admin UI — safe at plugins_loaded.
	BDSK_Export_Rest::init();
	BDSK_Media_Rest::init();
	BDSK_Event_Rest::init();
	BDSK_Cleanup::init();
	BDSK_Export_Job::init_scheduler();
	BDSK_Media_Index::init();
	BDSK_Event_Outbox::init();

	if ( is_admin() ) {
		BDSK_Settings_Page::init();
	}
} );

// Action Scheduler API (as_has_scheduled_action, as_schedule_recurring_action, etc.)
// must NOT be called before the AS data store is initialized.
// The AS docs explicitly state: call these at `init` or later.
add_action( 'init', static function () {
	if ( function_exists( 'as_has_scheduled_action' ) ) {
		BDSK_Cleanup::schedule_recurring();
		BDSK_Export_Job::schedule_heartbeat_check();
	}
}, 20 );
