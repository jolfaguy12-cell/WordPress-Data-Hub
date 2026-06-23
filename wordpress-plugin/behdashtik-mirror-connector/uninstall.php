<?php
/**
 * Runs when the plugin is deleted (not just deactivated).
 * Removes all plugin data: export archives, custom DB tables, and wp_options entries.
 */
if ( ! defined( 'WP_UNINSTALL_PLUGIN' ) ) {
	exit;
}

// ---------------------------------------------------------------------------
// Delete export archive directories from all candidate locations
// ---------------------------------------------------------------------------

$candidates = [];

// Configured private path (local_private_archive_mode)
if ( defined( 'BDSK_EXPORT_STORAGE_PATH' ) ) {
	$candidates[] = rtrim( BDSK_EXPORT_STORAGE_PATH, '/' ) . '/bdsk-exports';
}

// Legacy uploads fallback (may have files from pre-1.5.1 installations)
$upload = wp_upload_dir();
$candidates[] = $upload['basedir'] . '/bdsk-exports';

foreach ( $candidates as $dir ) {
	if ( is_dir( $dir ) ) {
		_bdsk_uninstall_rmdir( $dir );
	}
}

// ---------------------------------------------------------------------------
// Drop all custom tables
// ---------------------------------------------------------------------------

global $wpdb;

$tables = [
	$wpdb->prefix . 'bdsk_jobs',
	$wpdb->prefix . 'bdsk_request_log',
	$wpdb->prefix . 'bdsk_media_index',
	$wpdb->prefix . 'bdsk_event_outbox',
];
foreach ( $tables as $table ) {
	$wpdb->query( "DROP TABLE IF EXISTS `" . esc_sql( $table ) . "`" ); // phpcs:ignore WordPress.DB
}

// ---------------------------------------------------------------------------
// Delete all plugin options and transients
// ---------------------------------------------------------------------------

delete_option( 'bdsk_settings' );
delete_option( 'bdsk_stats' );
delete_option( 'bdsk_stats_totals' );
delete_option( 'bdsk_media_index_status' );

$wpdb->query( "DELETE FROM {$wpdb->options} WHERE option_name LIKE '_transient_bdsk_%'" );         // phpcs:ignore WordPress.DB
$wpdb->query( "DELETE FROM {$wpdb->options} WHERE option_name LIKE '_transient_timeout_bdsk_%'" ); // phpcs:ignore WordPress.DB

// ---------------------------------------------------------------------------
// Helper: recursive directory removal (no plugin classes available here)
// ---------------------------------------------------------------------------

function _bdsk_uninstall_rmdir( string $dir ): void {
	if ( ! is_dir( $dir ) ) {
		return;
	}
	$items = array_diff( scandir( $dir ), [ '.', '..' ] );
	foreach ( $items as $item ) {
		$path = $dir . '/' . $item;
		if ( is_dir( $path ) ) {
			_bdsk_uninstall_rmdir( $path );
		} else {
			unlink( $path );
		}
	}
	rmdir( $dir );
}
