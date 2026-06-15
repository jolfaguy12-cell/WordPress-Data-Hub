<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Event_Outbox {

	// ---------------------------------------------------------------------------
	// Initialization
	// ---------------------------------------------------------------------------

	public static function init(): void {
		// Order hooks (HPOS)
		add_action( 'woocommerce_new_order',             [ __CLASS__, 'handle_order_upserted' ] );
		add_action( 'woocommerce_update_order',          [ __CLASS__, 'handle_order_upserted' ] );
		add_action( 'woocommerce_order_status_changed',  [ __CLASS__, 'handle_order_status_changed' ], 10, 4 );
		add_action( 'woocommerce_trash_order',           [ __CLASS__, 'handle_order_deleted' ] );
		add_action( 'woocommerce_untrash_order',         [ __CLASS__, 'handle_order_upserted' ] );
		add_action( 'woocommerce_before_delete_order',   [ __CLASS__, 'handle_order_deleted' ] );
		add_action( 'woocommerce_reduce_order_stock',    [ __CLASS__, 'handle_order_stock_change' ] );
		add_action( 'woocommerce_restore_order_stock',   [ __CLASS__, 'handle_order_stock_change' ] );

		// Product hooks
		add_action( 'woocommerce_new_product',               [ __CLASS__, 'handle_product_upserted' ] );
		add_action( 'woocommerce_update_product',            [ __CLASS__, 'handle_product_upserted' ] );
		add_action( 'woocommerce_product_set_stock',         [ __CLASS__, 'handle_product_stock' ] );
		add_action( 'woocommerce_variation_set_stock',       [ __CLASS__, 'handle_variation_stock' ] );
		add_action( 'woocommerce_product_set_stock_status',  [ __CLASS__, 'handle_product_stock_status' ], 10, 2 );
		add_action( 'wp_trash_post',                         [ __CLASS__, 'handle_post_trashed' ] );
		add_action( 'untrashed_post',                        [ __CLASS__, 'handle_post_untrashed' ], 10, 2 );
		add_action( 'before_delete_post',                    [ __CLASS__, 'handle_post_before_delete' ] );
	}

	// ---------------------------------------------------------------------------
	// Core: enqueue with coalescing
	// ---------------------------------------------------------------------------

	/**
	 * Enqueues an event for the given entity.
	 * If a pending row for the same (entity_type, entity_id, event_type) already exists,
	 * only updates changed_at — ensuring repeated saves produce exactly ONE pending row.
	 */
	public static function enqueue( string $entity_type, int $entity_id, string $event_type ): void {
		if ( ! BDSK_Settings::get( 'event_sync_enabled', true ) ) {
			return;
		}
		if ( $entity_id <= 0 ) {
			return;
		}

		global $wpdb;
		$table = BDSK_DB::event_outbox_table();
		$now   = current_time( 'mysql', true );

		$existing_id = (int) $wpdb->get_var( $wpdb->prepare(
			"SELECT id FROM {$table}
			 WHERE entity_type = %s AND entity_id = %d AND event_type = %s AND status = 'pending'
			 LIMIT 1",
			$entity_type,
			$entity_id,
			$event_type
		) );

		if ( $existing_id > 0 ) {
			$wpdb->update( $table, [ 'changed_at' => $now ], [ 'id' => $existing_id ] );
			return;
		}

		$wpdb->insert( $table, [
			'event_id'    => wp_generate_uuid4(),
			'entity_type' => $entity_type,
			'entity_id'   => $entity_id,
			'event_type'  => $event_type,
			'changed_at'  => $now,
			'status'      => 'pending',
			'retry_count' => 0,
			'created_at'  => $now,
		] );
	}

	// ---------------------------------------------------------------------------
	// Order hook handlers
	// ---------------------------------------------------------------------------

	public static function handle_order_upserted( int $order_id ): void {
		self::enqueue( 'order', $order_id, 'upserted' );
	}

	// woocommerce_order_status_changed: ($order_id, $from_status, $to_status, $order)
	public static function handle_order_status_changed( int $order_id ): void {
		self::enqueue( 'order', $order_id, 'upserted' );
	}

	public static function handle_order_deleted( int $order_id ): void {
		self::enqueue( 'order', $order_id, 'deleted' );
	}

	// woocommerce_reduce_order_stock / woocommerce_restore_order_stock — receives WC_Order object
	public static function handle_order_stock_change( $order ): void {
		if ( ! method_exists( $order, 'get_items' ) ) {
			return;
		}
		foreach ( $order->get_items() as $item ) {
			if ( ! method_exists( $item, 'get_product_id' ) ) {
				continue;
			}
			$product_id = (int) $item->get_product_id();
			if ( $product_id > 0 ) {
				self::enqueue( 'product', $product_id, 'upserted' );
			}
		}
	}

	// ---------------------------------------------------------------------------
	// Product hook handlers
	// ---------------------------------------------------------------------------

	public static function handle_product_upserted( int $product_id ): void {
		self::enqueue( 'product', $product_id, 'upserted' );
	}

	// woocommerce_product_set_stock receives WC_Product object
	public static function handle_product_stock( $product ): void {
		if ( ! method_exists( $product, 'get_id' ) ) {
			return;
		}
		$parent_id  = method_exists( $product, 'get_parent_id' ) ? (int) $product->get_parent_id() : 0;
		$product_id = $parent_id > 0 ? $parent_id : (int) $product->get_id();
		self::enqueue( 'product', $product_id, 'upserted' );
	}

	// woocommerce_variation_set_stock receives WC_Product_Variation object
	public static function handle_variation_stock( $variation ): void {
		if ( ! method_exists( $variation, 'get_parent_id' ) ) {
			return;
		}
		$parent_id = (int) $variation->get_parent_id();
		if ( $parent_id > 0 ) {
			self::enqueue( 'product', $parent_id, 'upserted' );
		}
	}

	// woocommerce_product_set_stock_status: ($product_id, $stock_status, $product)
	public static function handle_product_stock_status( int $product_id ): void {
		self::enqueue( 'product', $product_id, 'upserted' );
	}

	// wp_trash_post fires for all post types — filter to products only
	public static function handle_post_trashed( int $post_id ): void {
		if ( 'product' === get_post_type( $post_id ) ) {
			self::enqueue( 'product', $post_id, 'deleted' );
		}
	}

	// untrashed_post: ($post_id, $previous_status)
	public static function handle_post_untrashed( int $post_id ): void {
		if ( 'product' === get_post_type( $post_id ) ) {
			self::enqueue( 'product', $post_id, 'upserted' );
		}
	}

	// before_delete_post fires before permanent deletion for all post types
	public static function handle_post_before_delete( int $post_id ): void {
		if ( 'product' === get_post_type( $post_id ) ) {
			self::enqueue( 'product', $post_id, 'deleted' );
		}
	}

	// ---------------------------------------------------------------------------
	// REST: pending event list (cursor-paginated)
	// ---------------------------------------------------------------------------

	public static function get_pending( int $after_id, int $limit ): array {
		global $wpdb;
		$table = BDSK_DB::event_outbox_table();
		$fetch = $limit + 1;

		$rows = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT id, event_id, entity_type, entity_id, event_type,
				        changed_at, status, retry_count, created_at
				 FROM {$table}
				 WHERE status = 'pending' AND id > %d
				 ORDER BY id ASC
				 LIMIT %d",
				$after_id,
				$fetch
			),
			ARRAY_A
		) ?: [];

		$has_more = count( $rows ) === $fetch;
		if ( $has_more ) {
			array_pop( $rows );
		}

		// Cast numeric fields
		foreach ( $rows as &$row ) {
			$row['id']          = (int) $row['id'];
			$row['entity_id']   = (int) $row['entity_id'];
			$row['retry_count'] = (int) $row['retry_count'];
		}
		unset( $row );

		return [
			'items'         => $rows,
			'has_more'      => $has_more,
			'next_after_id' => $has_more ? (int) end( $rows )['id'] : null,
		];
	}

	// ---------------------------------------------------------------------------
	// REST: acknowledge events
	// ---------------------------------------------------------------------------

	public static function ack( array $event_ids ): int {
		if ( empty( $event_ids ) ) {
			return 0;
		}

		global $wpdb;
		$table        = BDSK_DB::event_outbox_table();
		$now          = current_time( 'mysql', true );
		$placeholders = implode( ',', array_fill( 0, count( $event_ids ), '%s' ) );

		return (int) $wpdb->query(
			$wpdb->prepare(
				"UPDATE {$table}
				 SET status = 'acknowledged', acknowledged_at = %s
				 WHERE event_id IN ({$placeholders})
				 AND status IN ('pending','sent')",
				array_merge( [ $now ], $event_ids )
			)
		);
	}

	// ---------------------------------------------------------------------------
	// Stats (for health endpoint and settings page)
	// ---------------------------------------------------------------------------

	public static function get_stats(): array {
		global $wpdb;
		$table = BDSK_DB::event_outbox_table();

		$rows = $wpdb->get_results(
			"SELECT status, COUNT(*) AS cnt FROM {$table} GROUP BY status",
			ARRAY_A
		) ?: [];

		$counts = [ 'pending' => 0, 'sent' => 0, 'acknowledged' => 0, 'expired' => 0 ];
		foreach ( $rows as $row ) {
			$counts[ $row['status'] ] = (int) $row['cnt'];
		}

		$last_event_at = $wpdb->get_var( "SELECT MAX(created_at) FROM {$table}" );
		$last_ack_at   = $wpdb->get_var(
			"SELECT MAX(acknowledged_at) FROM {$table} WHERE acknowledged_at IS NOT NULL"
		);

		return array_merge( $counts, [
			'last_event_at' => $last_event_at,
			'last_ack_at'   => $last_ack_at,
		] );
	}

	// ---------------------------------------------------------------------------
	// Cleanup: prune old acknowledged events
	// ---------------------------------------------------------------------------

	public static function prune_old_acknowledged(): void {
		global $wpdb;
		$wpdb->query(
			"DELETE FROM " . BDSK_DB::event_outbox_table() . "
			 WHERE status = 'acknowledged'
			 AND acknowledged_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)"
		);
	}
}
