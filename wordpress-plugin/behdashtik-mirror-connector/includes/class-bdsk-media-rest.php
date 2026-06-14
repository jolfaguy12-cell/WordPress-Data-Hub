<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Media_Rest {

	private const NAMESPACE = 'behdashtik-connector/v1';

	public static function init(): void {
		add_action( 'rest_api_init', [ __CLASS__, 'register_routes' ] );
	}

	public static function register_routes(): void {
		register_rest_route( self::NAMESPACE, '/media-manifest', [
			'methods'             => WP_REST_Server::READABLE,
			'callback'            => [ __CLASS__, 'handle_manifest' ],
			'permission_callback' => '__return_true',
			'args'                => [
				'after_id' => [
					'default'           => 0,
					'sanitize_callback' => 'absint',
				],
				'limit' => [
					'default'           => 200,
					'sanitize_callback' => 'absint',
					'validate_callback' => fn( $v ) => (int) $v >= 1 && (int) $v <= 500,
				],
				'modified_since' => [
					'default'           => null,
					'sanitize_callback' => 'absint',
				],
				'include_deleted' => [
					'default'           => 'true',
					'sanitize_callback' => 'sanitize_text_field',
				],
			],
		] );
	}

	// ---------------------------------------------------------------------------
	// GET /media-manifest
	// ---------------------------------------------------------------------------

	public static function handle_manifest( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}

		if ( ! BDSK_Settings::get( 'media_manifest_enabled', true ) ) {
			return new WP_Error( 'media_manifest_disabled', 'Media manifest is disabled.', [ 'status' => 403 ] );
		}

		global $wpdb;
		$tbl = BDSK_DB::media_index_table();

		$after_id       = (int) $request->get_param( 'after_id' );
		$limit          = min( 500, max( 1, (int) $request->get_param( 'limit' ) ) );
		$modified_since = $request->get_param( 'modified_since' );
		$inc_deleted    = 'false' !== strtolower( (string) $request->get_param( 'include_deleted' ) );

		// Build WHERE clauses
		$wheres = [ 'id > ' . (int) $after_id ];

		if ( $modified_since && (int) $modified_since > 0 ) {
			$ts       = gmdate( 'Y-m-d H:i:s', (int) $modified_since );
			$wheres[] = $wpdb->prepare( 'index_updated_at >= %s', $ts );
		}

		if ( ! $inc_deleted ) {
			$wheres[] = "status = 'active'";
		}

		$where_sql = implode( ' AND ', $wheres );

		// Fetch limit + 1 to detect has_more
		$fetch = $limit + 1;

		// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		$rows = $wpdb->get_results(
			"SELECT id, attachment_id, product_id, order_id, image_type,
			        original_url, alt_text, title, caption,
			        width, height, mime_type, file_size,
			        attachment_modified_at, index_updated_at, status
			 FROM {$tbl}
			 WHERE {$where_sql}
			 ORDER BY id ASC
			 LIMIT {$fetch}",
			ARRAY_A
		);
		// phpcs:enable

		$has_more     = count( $rows ) > $limit;
		$items        = array_slice( $rows, 0, $limit );
		$next_after   = $has_more ? (int) end( $items )['id'] : null;

		$output_items = array_map( [ __CLASS__, 'format_row' ], $items );

		return new WP_REST_Response( [
			'items'         => $output_items,
			'next_after_id' => $next_after,
			'has_more'      => $has_more,
		], 200 );
	}

	// ---------------------------------------------------------------------------
	// Row formatter — converts DB row to API shape
	// ---------------------------------------------------------------------------

	private static function format_row( array $row ): array {
		return [
			'id'            => (int) $row['id'],
			'attachment_id' => (int) $row['attachment_id'],
			'product_id'    => (int) $row['product_id'] > 0 ? (int) $row['product_id'] : null,
			'order_id'      => (int) $row['order_id']   > 0 ? (int) $row['order_id']   : null,
			'image_type'    => $row['image_type'],
			'original_url'  => $row['original_url'],
			'alt_text'      => $row['alt_text'],
			'title'         => $row['title'],
			'caption'       => $row['caption'],
			'width'         => null !== $row['width']     ? (int) $row['width']     : null,
			'height'        => null !== $row['height']    ? (int) $row['height']    : null,
			'mime_type'     => $row['mime_type'],
			'file_size'     => null !== $row['file_size'] ? (int) $row['file_size'] : null,
			'modified_at'   => '1970-01-01T00:00:00Z' !== $row['attachment_modified_at']
				? gmdate( 'c', strtotime( $row['attachment_modified_at'] ) )
				: null,
			'status'        => $row['status'],
		];
	}
}
