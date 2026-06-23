<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Health {

	public static function get_data(): array {
		global $wpdb;

		// WooCommerce detection
		$wc_active  = class_exists( 'WooCommerce' );
		$wc_version = $wc_active && defined( 'WC_VERSION' ) ? WC_VERSION : null;

		// DB version (MySQL or MariaDB)
		$db_version = $wpdb->get_var( 'SELECT VERSION()' );

		// gzip / zlib availability
		$gzip_available = function_exists( 'gzopen' ) && function_exists( 'gzencode' );

		// OpenSSL availability (required for encrypted-at-rest API key storage)
		$openssl_available = extension_loaded( 'openssl' );

		// PHP memory limit and max_execution_time
		$memory_limit   = ini_get( 'memory_limit' );
		$max_exec_time  = (int) ini_get( 'max_execution_time' );

		$settings = BDSK_Settings::all();

		$media_status = BDSK_Media_Index::get_status();

		$export_storage_error = BDSK_Export_Job::export_storage_error();

		return [
			'status'                   => null !== $export_storage_error ? 'error' : 'ok',
			'site_url'                 => get_site_url(),
			'plugin_version'           => BDSK_VERSION,
			'wordpress_version'        => get_bloginfo( 'version' ),
			'woocommerce_active'       => $wc_active,
			'woocommerce_version'      => $wc_version,
			'php_version'              => PHP_VERSION,
			'mysql_or_mariadb_version' => $db_version,
			'gzip_or_zlib_available'   => $gzip_available,
			'openssl_available'        => $openssl_available,
			'memory_limit'             => $memory_limit,
			'max_execution_time'       => $max_exec_time,
			'server_time'              => gmdate( 'c' ),
			'db_prefix'                => $wpdb->prefix,
			'connector_enabled'        => (bool) $settings['enabled'],
			'read_mode_status'         => $settings['read_access_enabled'] ? 'on' : 'off',
			'write_mode_status'        => 'off',
			'backup_export_enabled'    => (bool) $settings['backup_export_enabled'],
			'media_manifest_enabled'     => (bool) BDSK_Settings::get( 'media_manifest_enabled', true ),
			'media_index_status'         => null === $media_status['last_full_build_at'] ? 'never_built' : $media_status['status'],
			'media_index_last_built_at'  => $media_status['last_full_build_at'],
			'event_sync_enabled'         => (bool) BDSK_Settings::get( 'event_sync_enabled', true ),
			'event_outbox_pending_count' => (int) ( BDSK_Event_Outbox::get_stats()['pending'] ?? 0 ),
			'export_mode'                => BDSK_Export_Job::get_export_mode(),
			'export_storage_error'       => $export_storage_error,
			'last_successful_request'    => BDSK_Stats::get_totals()['last_successful_at'] ?? null,
			'last_cleanup_run'           => BDSK_Stats::get_cleanup_status()['last_run_at'] ?? null,
		];
	}
}
