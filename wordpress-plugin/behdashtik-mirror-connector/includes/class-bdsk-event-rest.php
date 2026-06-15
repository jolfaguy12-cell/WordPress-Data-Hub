<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Event_Rest {

	private const NAMESPACE = 'behdashtik-connector/v1';

	public static function init(): void {
		add_action( 'rest_api_init', [ __CLASS__, 'register_routes' ] );
	}

	public static function register_routes(): void {
		register_rest_route( self::NAMESPACE, '/events/pending', [
			'methods'             => 'GET',
			'callback'            => [ __CLASS__, 'handle_pending' ],
			'permission_callback' => '__return_true',
			'args'                => [
				'after_id' => [
					'type'              => 'integer',
					'default'           => 0,
					'minimum'           => 0,
					'sanitize_callback' => 'absint',
				],
				'limit'    => [
					'type'              => 'integer',
					'default'           => 200,
					'minimum'           => 1,
					'maximum'           => 500,
					'sanitize_callback' => 'absint',
				],
			],
		] );

		register_rest_route( self::NAMESPACE, '/events/ack', [
			'methods'             => 'POST',
			'callback'            => [ __CLASS__, 'handle_ack' ],
			'permission_callback' => '__return_true',
		] );

		register_rest_route( self::NAMESPACE, '/snapshot/order/(?P<order_id>\d+)', [
			'methods'             => 'GET',
			'callback'            => [ __CLASS__, 'handle_order_snapshot' ],
			'permission_callback' => '__return_true',
			'args'                => [
				'order_id' => [
					'type'              => 'integer',
					'minimum'           => 1,
					'sanitize_callback' => 'absint',
				],
			],
		] );

		register_rest_route( self::NAMESPACE, '/snapshot/product/(?P<product_id>\d+)', [
			'methods'             => 'GET',
			'callback'            => [ __CLASS__, 'handle_product_snapshot' ],
			'permission_callback' => '__return_true',
			'args'                => [
				'product_id' => [
					'type'              => 'integer',
					'minimum'           => 1,
					'sanitize_callback' => 'absint',
				],
			],
		] );
	}

	// ---------------------------------------------------------------------------
	// GET /events/pending
	// ---------------------------------------------------------------------------

	public static function handle_pending( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}
		if ( ! BDSK_Settings::get( 'event_sync_enabled', true ) ) {
			return new WP_Error( 'event_sync_disabled', 'Event sync is disabled.', [ 'status' => 403 ] );
		}

		$result = BDSK_Event_Outbox::get_pending(
			(int) $request->get_param( 'after_id' ),
			(int) $request->get_param( 'limit' )
		);

		return new WP_REST_Response( $result, 200 );
	}

	// ---------------------------------------------------------------------------
	// POST /events/ack
	// ---------------------------------------------------------------------------

	public static function handle_ack( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}
		if ( ! BDSK_Settings::get( 'event_sync_enabled', true ) ) {
			return new WP_Error( 'event_sync_disabled', 'Event sync is disabled.', [ 'status' => 403 ] );
		}

		$body      = $request->get_json_params();
		$event_ids = isset( $body['event_ids'] ) && is_array( $body['event_ids'] )
			? array_map( 'sanitize_text_field', $body['event_ids'] )
			: [];

		if ( empty( $event_ids ) ) {
			return new WP_Error( 'missing_event_ids', 'event_ids array is required.', [ 'status' => 400 ] );
		}

		$acknowledged = BDSK_Event_Outbox::ack( $event_ids );

		return new WP_REST_Response( [ 'acknowledged' => $acknowledged ], 200 );
	}

	// ---------------------------------------------------------------------------
	// GET /snapshot/order/{order_id}
	// ---------------------------------------------------------------------------

	public static function handle_order_snapshot( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}
		if ( ! BDSK_Settings::get( 'event_sync_enabled', true ) ) {
			return new WP_Error( 'event_sync_disabled', 'Event sync is disabled.', [ 'status' => 403 ] );
		}

		global $wpdb;
		$order_id = (int) $request->get_param( 'order_id' );

		$order_row = $wpdb->get_row(
			$wpdb->prepare(
				"SELECT * FROM {$wpdb->prefix}wc_orders WHERE id = %d",
				$order_id
			),
			ARRAY_A
		);

		if ( null === $order_row ) {
			return new WP_REST_Response( [ 'exists' => false ], 404 );
		}

		// Meta rows (all, raw values)
		$meta = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT meta_key, meta_value FROM {$wpdb->prefix}wc_orders_meta WHERE order_id = %d",
				$order_id
			),
			ARRAY_A
		) ?: [];

		// Order items
		$item_rows = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT * FROM {$wpdb->prefix}woocommerce_order_items WHERE order_id = %d ORDER BY order_item_id ASC",
				$order_id
			),
			ARRAY_A
		) ?: [];

		// Batch-fetch all itemmeta in one query
		$items = [];
		if ( ! empty( $item_rows ) ) {
			$item_ids     = array_column( $item_rows, 'order_item_id' );
			$placeholders = implode( ',', array_fill( 0, count( $item_ids ), '%d' ) );
			$itemmeta_rows = $wpdb->get_results(
				$wpdb->prepare(
					"SELECT order_item_id, meta_key, meta_value
					 FROM {$wpdb->prefix}woocommerce_order_itemmeta
					 WHERE order_item_id IN ({$placeholders})
					 ORDER BY order_item_id ASC, meta_id ASC",
					...$item_ids
				),
				ARRAY_A
			) ?: [];

			// Group itemmeta by order_item_id
			$itemmeta_by_item = [];
			foreach ( $itemmeta_rows as $m ) {
				$itemmeta_by_item[ (int) $m['order_item_id'] ][] = [
					'meta_key'   => $m['meta_key'],
					'meta_value' => $m['meta_value'],
				];
			}

			foreach ( $item_rows as $item_row ) {
				$iid     = (int) $item_row['order_item_id'];
				$items[] = [
					'item_row' => $item_row,
					'itemmeta' => $itemmeta_by_item[ $iid ] ?? [],
				];
			}
		}

		return new WP_REST_Response( [
			'order_id'  => $order_id,
			'order_row' => $order_row,
			'meta'      => $meta,
			'items'     => $items,
			'exists'    => true,
		], 200 );
	}

	// ---------------------------------------------------------------------------
	// GET /snapshot/product/{product_id}
	// ---------------------------------------------------------------------------

	public static function handle_product_snapshot( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}
		if ( ! BDSK_Settings::get( 'event_sync_enabled', true ) ) {
			return new WP_Error( 'event_sync_disabled', 'Event sync is disabled.', [ 'status' => 403 ] );
		}

		global $wpdb;
		$product_id = (int) $request->get_param( 'product_id' );

		$post_row = $wpdb->get_row(
			$wpdb->prepare( "SELECT * FROM {$wpdb->posts} WHERE ID = %d", $product_id ),
			ARRAY_A
		);

		if ( null === $post_row ) {
			return new WP_REST_Response( [ 'exists' => false ], 404 );
		}

		// All postmeta (raw values — do NOT unserialize)
		$meta = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT meta_key, meta_value FROM {$wpdb->postmeta} WHERE post_id = %d ORDER BY meta_id ASC",
				$product_id
			),
			ARRAY_A
		) ?: [];

		// Term relationships grouped by taxonomy
		$term_rows = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT tt.taxonomy, tt.term_id
				 FROM {$wpdb->term_relationships} tr
				 JOIN {$wpdb->term_taxonomy} tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
				 WHERE tr.object_id = %d
				 ORDER BY tt.taxonomy ASC, tt.term_id ASC",
				$product_id
			),
			ARRAY_A
		) ?: [];

		$terms_by_tax = [];
		foreach ( $term_rows as $t ) {
			$terms_by_tax[ $t['taxonomy'] ][] = (int) $t['term_id'];
		}
		$terms = [];
		foreach ( $terms_by_tax as $taxonomy => $term_ids ) {
			$terms[] = [ 'taxonomy' => $taxonomy, 'term_ids' => $term_ids ];
		}

		// Product lookup row
		$lookup_row = $wpdb->get_row(
			$wpdb->prepare(
				"SELECT * FROM {$wpdb->prefix}wc_product_meta_lookup WHERE product_id = %d",
				$product_id
			),
			ARRAY_A
		);

		// Variations — batch all queries
		$var_ids = $wpdb->get_col(
			$wpdb->prepare(
				"SELECT ID FROM {$wpdb->posts}
				 WHERE post_parent = %d AND post_type = 'product_variation'
				 ORDER BY ID ASC",
				$product_id
			)
		) ?: [];

		$variations = [];
		if ( ! empty( $var_ids ) ) {
			$var_ids    = array_map( 'intval', $var_ids );
			$var_ph     = implode( ',', array_fill( 0, count( $var_ids ), '%d' ) );

			// Variation posts
			$var_post_rows = $wpdb->get_results(
				$wpdb->prepare(
					"SELECT * FROM {$wpdb->posts} WHERE ID IN ({$var_ph}) ORDER BY ID ASC",
					...$var_ids
				),
				ARRAY_A
			) ?: [];

			// Variation postmeta (one query for all variations)
			$var_meta_rows = $wpdb->get_results(
				$wpdb->prepare(
					"SELECT post_id, meta_key, meta_value FROM {$wpdb->postmeta}
					 WHERE post_id IN ({$var_ph}) ORDER BY post_id ASC, meta_id ASC",
					...$var_ids
				),
				ARRAY_A
			) ?: [];

			// Variation lookup rows
			$var_lookup_rows = $wpdb->get_results(
				$wpdb->prepare(
					"SELECT * FROM {$wpdb->prefix}wc_product_meta_lookup
					 WHERE product_id IN ({$var_ph}) ORDER BY product_id ASC",
					...$var_ids
				),
				ARRAY_A
			) ?: [];

			// Group by variation ID
			$var_meta_by_id   = [];
			foreach ( $var_meta_rows as $m ) {
				$var_meta_by_id[ (int) $m['post_id'] ][] = [
					'meta_key'   => $m['meta_key'],
					'meta_value' => $m['meta_value'],
				];
			}
			$var_lookup_by_id = [];
			foreach ( $var_lookup_rows as $l ) {
				$var_lookup_by_id[ (int) $l['product_id'] ] = $l;
			}

			foreach ( $var_post_rows as $vp ) {
				$vid        = (int) $vp['ID'];
				$variations[] = [
					'post_row'   => $vp,
					'meta'       => $var_meta_by_id[ $vid ] ?? [],
					'lookup_row' => $var_lookup_by_id[ $vid ] ?? null,
				];
			}
		}

		return new WP_REST_Response( [
			'product_id' => $product_id,
			'post_row'   => $post_row,
			'meta'       => $meta,
			'terms'      => $terms,
			'lookup_row' => $lookup_row,
			'variations' => $variations,
			'exists'     => true,
		], 200 );
	}
}
