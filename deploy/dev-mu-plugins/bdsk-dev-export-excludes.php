<?php
/**
 * Behdashtik Mirror Connector — dev-only export table exclusions.
 *
 * Excludes operational Action Scheduler queue/history tables from the DB
 * mirror export. These are runtime state for WooCommerce's background jobs
 * and are not needed in the read-only mirror.
 *
 * Uses the connector's official `bdsk_export_tables` filter — no core edits.
 *
 * Canonical source: /root/wordpress-data-hub/deploy/dev-mu-plugins/
 * Deployed to:      <dev>/wp-content/mu-plugins/  (dev environment only)
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

add_filter( 'bdsk_export_tables', static function ( array $tables ): array {
	return array_values( array_filter(
		$tables,
		static fn( string $table ): bool => ! str_starts_with( $table, 'wp_actionscheduler_' )
	) );
} );
