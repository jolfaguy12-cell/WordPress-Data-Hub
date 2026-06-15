<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Media_Index {

	private const STATUS_OPTION = 'bdsk_media_index_status';
	private const AS_CHUNK_HOOK = 'bdsk_media_index_chunk';
	private const CHUNK_SIZE    = 200;

	// ---------------------------------------------------------------------------
	// Lifecycle
	// ---------------------------------------------------------------------------

	public static function init(): void {
		add_action( self::AS_CHUNK_HOOK, [ __CLASS__, 'run_chunk' ] );

		// Incremental hooks — fired by WordPress when content changes
		add_action( 'save_post_product',           [ __CLASS__, 'handle_save_product' ] );
		add_action( 'save_post_product_variation', [ __CLASS__, 'handle_save_product' ] );
		add_action( 'add_attachment',              [ __CLASS__, 'handle_attachment_change' ] );
		add_action( 'edit_attachment',             [ __CLASS__, 'handle_attachment_change' ] );
		add_action( 'delete_attachment',           [ __CLASS__, 'handle_delete_attachment' ] );

		// HPOS order saves
		add_action( 'woocommerce_after_order_object_save', [ __CLASS__, 'handle_order_save' ] );
	}

	// ---------------------------------------------------------------------------
	// Status option helpers
	// ---------------------------------------------------------------------------

	public static function get_status(): array {
		$defaults = [
			'status'             => 'idle',
			'current_step'       => 'done',
			'current_offset'     => 0,
			'started_at'         => null,
			'finished_at'        => null,
			'last_error'         => null,
			'last_full_build_at' => null,
		];
		$stored = get_option( self::STATUS_OPTION, [] );
		return array_merge( $defaults, is_array( $stored ) ? $stored : [] );
	}

	private static function set_status( array $data ): void {
		$current = self::get_status();
		update_option( self::STATUS_OPTION, array_merge( $current, $data ), false );
	}

	// ---------------------------------------------------------------------------
	// Full index build — schedule first chunk
	// ---------------------------------------------------------------------------

	public static function schedule_full_build(): string|WP_Error {
		if ( ! function_exists( 'as_enqueue_async_action' ) ) {
			return new WP_Error( 'as_missing', 'Action Scheduler not available.', [ 'status' => 500 ] );
		}

		$status = self::get_status();
		if ( 'running' === $status['status'] ) {
			return new WP_Error( 'already_running', 'Media index build is already running.', [ 'status' => 409 ] );
		}

		self::set_status( [
			'status'         => 'running',
			'current_step'   => 'products',
			'current_offset' => 0,
			'started_at'     => gmdate( 'c' ),
			'finished_at'    => null,
			'last_error'     => null,
		] );

		as_enqueue_async_action( self::AS_CHUNK_HOOK, [], 'bdsk' );

		bdsk_log( 'Media index full build scheduled.' );
		return 'scheduled';
	}

	// ---------------------------------------------------------------------------
	// Action Scheduler callback — processes one chunk
	// ---------------------------------------------------------------------------

	public static function run_chunk(): void {
		$status = self::get_status();

		if ( 'running' !== $status['status'] ) {
			return;
		}

		$step   = $status['current_step'];
		$offset = (int) $status['current_offset'];

		try {
			switch ( $step ) {
				case 'products':
					$done = self::process_products_chunk( $offset );
					if ( $done ) {
						self::set_status( [ 'current_step' => 'variations', 'current_offset' => 0 ] );
					} else {
						self::set_status( [ 'current_offset' => $offset + self::CHUNK_SIZE ] );
					}
					as_enqueue_async_action( self::AS_CHUNK_HOOK, [], 'bdsk' );
					break;

				case 'variations':
					$done = self::process_variations_chunk( $offset );
					if ( $done ) {
						$evidence_keys = trim( BDSK_Settings::get( 'evidence_meta_keys', '' ) );
						$next_step = ( $evidence_keys && BDSK_Settings::get( 'include_evidence_images', true ) )
							? 'evidence'
							: ( BDSK_Settings::get( 'index_unknown_media', false ) ? 'unknown' : 'done' );
						self::set_status( [ 'current_step' => $next_step, 'current_offset' => 0 ] );
					} else {
						self::set_status( [ 'current_offset' => $offset + self::CHUNK_SIZE ] );
					}
					as_enqueue_async_action( self::AS_CHUNK_HOOK, [], 'bdsk' );
					break;

				case 'evidence':
					$done = self::process_evidence_chunk( $offset );
					if ( $done ) {
						$next_step = BDSK_Settings::get( 'index_unknown_media', false ) ? 'unknown' : 'done';
						self::set_status( [ 'current_step' => $next_step, 'current_offset' => 0 ] );
					} else {
						self::set_status( [ 'current_offset' => $offset + self::CHUNK_SIZE ] );
					}
					as_enqueue_async_action( self::AS_CHUNK_HOOK, [], 'bdsk' );
					break;

				case 'unknown':
					$done = self::process_unknown_chunk( $offset );
					if ( $done ) {
						self::set_status( [ 'current_step' => 'done', 'current_offset' => 0 ] );
					} else {
						self::set_status( [ 'current_offset' => $offset + self::CHUNK_SIZE ] );
					}
					as_enqueue_async_action( self::AS_CHUNK_HOOK, [], 'bdsk' );
					break;

				case 'done':
				default:
					$now = gmdate( 'c' );
					self::set_status( [
						'status'             => 'idle',
						'current_step'       => 'done',
						'current_offset'     => 0,
						'finished_at'        => $now,
						'last_full_build_at' => $now,
					] );
					bdsk_log( 'Media index full build complete.' );
					break;
			}
		} catch ( \Throwable $e ) {
			self::set_status( [
				'status'     => 'idle',
				'last_error' => $e->getMessage(),
			] );
			bdsk_log( 'Media index build error: ' . $e->getMessage() );
		}
	}

	// ---------------------------------------------------------------------------
	// Build step: products (main image + gallery)
	// ---------------------------------------------------------------------------

	private static function process_products_chunk( int $offset ): bool {
		global $wpdb;

		$posts = $wpdb->get_results( $wpdb->prepare(
			"SELECT ID FROM {$wpdb->posts}
			 WHERE post_type = 'product' AND post_status NOT IN ('auto-draft','trash')
			 ORDER BY ID ASC LIMIT %d OFFSET %d",
			self::CHUNK_SIZE,
			$offset
		) );

		if ( empty( $posts ) ) {
			return true; // done
		}

		foreach ( $posts as $post ) {
			$product_id = (int) $post->ID;

			// Main thumbnail
			$thumb_id = (int) get_post_meta( $product_id, '_thumbnail_id', true );
			if ( $thumb_id > 0 ) {
				self::upsert( $thumb_id, $product_id, 0, 'main' );
			}

			// Gallery images
			$gallery_raw = get_post_meta( $product_id, '_product_image_gallery', true );
			if ( $gallery_raw ) {
				foreach ( array_filter( array_map( 'intval', explode( ',', $gallery_raw ) ) ) as $att_id ) {
					if ( $att_id > 0 ) {
						self::upsert( $att_id, $product_id, 0, 'gallery' );
					}
				}
			}

			// Remove rows for images that were removed from this product
			self::prune_product_rows( $product_id, $thumb_id, array_filter( array_map( 'intval', $gallery_raw ? explode( ',', $gallery_raw ) : [] ) ) );
		}

		return false;
	}

	// ---------------------------------------------------------------------------
	// Build step: product_variation (variation image)
	// ---------------------------------------------------------------------------

	private static function process_variations_chunk( int $offset ): bool {
		global $wpdb;

		$posts = $wpdb->get_results( $wpdb->prepare(
			"SELECT ID FROM {$wpdb->posts}
			 WHERE post_type = 'product_variation' AND post_status NOT IN ('auto-draft','trash')
			 ORDER BY ID ASC LIMIT %d OFFSET %d",
			self::CHUNK_SIZE,
			$offset
		) );

		if ( empty( $posts ) ) {
			return true;
		}

		foreach ( $posts as $post ) {
			$variation_id = (int) $post->ID;
			$thumb_id     = (int) get_post_meta( $variation_id, '_thumbnail_id', true );
			if ( $thumb_id > 0 ) {
				self::upsert( $thumb_id, $variation_id, 0, 'variation' );
			}
		}

		return false;
	}

	// ---------------------------------------------------------------------------
	// Build step: evidence images (order meta → attachment IDs)
	// ---------------------------------------------------------------------------

	private static function process_evidence_chunk( int $offset ): bool {
		global $wpdb;

		$meta_keys_raw = trim( BDSK_Settings::get( 'evidence_meta_keys', '' ) );
		if ( '' === $meta_keys_raw ) {
			return true; // nothing to do
		}

		$meta_keys = array_filter( array_map( 'trim', explode( ',', $meta_keys_raw ) ) );
		if ( empty( $meta_keys ) ) {
			return true;
		}

		$hpos = self::is_hpos_active();

		if ( $hpos ) {
			$placeholders = implode( ',', array_fill( 0, count( $meta_keys ), '%s' ) );
			$query_args   = array_merge( $meta_keys, [ self::CHUNK_SIZE, $offset ] );

			$rows = $wpdb->get_results( $wpdb->prepare(
				"SELECT DISTINCT order_id, meta_key, meta_value
				 FROM {$wpdb->prefix}wc_orders_meta
				 WHERE meta_key IN ({$placeholders}) AND meta_value REGEXP '^[0-9]+$'
				 ORDER BY order_id ASC LIMIT %d OFFSET %d",
				...$query_args
			) );
		} else {
			$placeholders = implode( ',', array_fill( 0, count( $meta_keys ), '%s' ) );
			$query_args   = array_merge( $meta_keys, [ self::CHUNK_SIZE, $offset ] );

			$rows = $wpdb->get_results( $wpdb->prepare(
				"SELECT pm.post_id AS order_id, pm.meta_key, pm.meta_value
				 FROM {$wpdb->postmeta} pm
				 JOIN {$wpdb->posts} p ON p.ID = pm.post_id
				 WHERE p.post_type IN ('shop_order','shop_order_refund')
				 AND pm.meta_key IN ({$placeholders}) AND pm.meta_value REGEXP '^[0-9]+$'
				 ORDER BY pm.post_id ASC LIMIT %d OFFSET %d",
				...$query_args
			) );
		}

		if ( empty( $rows ) ) {
			return true;
		}

		foreach ( $rows as $row ) {
			$att_id  = (int) $row->meta_value;
			$post    = get_post( $att_id );
			if ( $post && 'attachment' === $post->post_type ) {
				self::upsert( $att_id, 0, (int) $row->order_id, 'evidence' );
			}
		}

		return false;
	}

	// ---------------------------------------------------------------------------
	// Build step: unknown attachments not matched by any other rule
	// ---------------------------------------------------------------------------

	private static function process_unknown_chunk( int $offset ): bool {
		global $wpdb;

		$tbl  = BDSK_DB::media_index_table();

		$posts = $wpdb->get_results( $wpdb->prepare(
			"SELECT ID FROM {$wpdb->posts} att
			 WHERE att.post_type = 'attachment'
			 AND att.post_status != 'trash'
			 AND NOT EXISTS (
			   SELECT 1 FROM {$tbl} mi WHERE mi.attachment_id = att.ID AND mi.status = 'active'
			 )
			 ORDER BY att.ID ASC LIMIT %d OFFSET %d",
			self::CHUNK_SIZE,
			$offset
		) );

		if ( empty( $posts ) ) {
			return true;
		}

		foreach ( $posts as $post ) {
			self::upsert( (int) $post->ID, 0, 0, 'unknown' );
		}

		return false;
	}

	// ---------------------------------------------------------------------------
	// Core upsert — idempotent, used by both full build and incremental hooks
	// ---------------------------------------------------------------------------

	public static function upsert( int $attachment_id, int $product_id, int $order_id, string $image_type ): void {
		global $wpdb;

		$post = get_post( $attachment_id );
		if ( ! $post || 'attachment' !== $post->post_type ) {
			return;
		}

		$url      = wp_get_attachment_url( $attachment_id ) ?: '';
		$meta     = wp_get_attachment_metadata( $attachment_id );
		$alt_text = get_post_meta( $attachment_id, '_wp_attachment_image_alt', true );

		$width     = isset( $meta['width'] )  ? (int) $meta['width']  : null;
		$height    = isset( $meta['height'] ) ? (int) $meta['height'] : null;
		$file_size = null;
		if ( isset( $meta['file'] ) ) {
			$upload_dir = wp_upload_dir();
			$full_path  = trailingslashit( $upload_dir['basedir'] ) . $meta['file'];
			if ( file_exists( $full_path ) ) {
				$file_size = (int) filesize( $full_path );
			}
		}

		$row = [
			'attachment_id'          => $attachment_id,
			'product_id'             => $product_id,
			'order_id'               => $order_id,
			'image_type'             => $image_type,
			'original_url'           => $url,
			'alt_text'               => $alt_text ?: null,
			'title'                  => $post->post_title ?: null,
			'caption'                => $post->post_excerpt ?: null,
			'width'                  => $width,
			'height'                 => $height,
			'mime_type'              => $post->post_mime_type ?: null,
			'file_size'              => $file_size,
			'attachment_modified_at' => get_post_modified_time( 'Y-m-d H:i:s', true, $post ) ?: '1970-01-01 00:00:00',
			'index_updated_at'       => current_time( 'mysql', true ),
			'status'                 => 'active',
		];

		// phpcs:disable WordPress.DB.PreparedSQL.NotPrepared
		$wpdb->query(
			"INSERT INTO " . BDSK_DB::media_index_table() . " (
				attachment_id, product_id, order_id, image_type,
				original_url, alt_text, title, caption,
				width, height, mime_type, file_size,
				attachment_modified_at, index_updated_at, status
			) VALUES (
				" . (int) $row['attachment_id'] . ",
				" . (int) $row['product_id'] . ",
				" . (int) $row['order_id'] . ",
				'" . esc_sql( $row['image_type'] ) . "',
				'" . esc_sql( $row['original_url'] ) . "',
				" . ( null === $row['alt_text']   ? 'NULL' : "'" . esc_sql( $row['alt_text'] )   . "'" ) . ",
				" . ( null === $row['title']      ? 'NULL' : "'" . esc_sql( $row['title'] )       . "'" ) . ",
				" . ( null === $row['caption']    ? 'NULL' : "'" . esc_sql( $row['caption'] )     . "'" ) . ",
				" . ( null === $row['width']      ? 'NULL' : (int) $row['width'] ) . ",
				" . ( null === $row['height']     ? 'NULL' : (int) $row['height'] ) . ",
				" . ( null === $row['mime_type']  ? 'NULL' : "'" . esc_sql( $row['mime_type'] )  . "'" ) . ",
				" . ( null === $row['file_size']  ? 'NULL' : (int) $row['file_size'] ) . ",
				'" . esc_sql( $row['attachment_modified_at'] ) . "',
				'" . esc_sql( $row['index_updated_at'] ) . "',
				'active'
			)
			ON DUPLICATE KEY UPDATE
				original_url           = VALUES(original_url),
				alt_text               = VALUES(alt_text),
				title                  = VALUES(title),
				caption                = VALUES(caption),
				width                  = VALUES(width),
				height                 = VALUES(height),
				mime_type              = VALUES(mime_type),
				file_size              = VALUES(file_size),
				attachment_modified_at = VALUES(attachment_modified_at),
				index_updated_at       = VALUES(index_updated_at),
				status                 = 'active'"
		);
		// phpcs:enable
	}

	// ---------------------------------------------------------------------------
	// Prune rows for images removed from a product's main/gallery set
	// ---------------------------------------------------------------------------

	private static function prune_product_rows( int $product_id, int $thumb_id, array $gallery_ids ): void {
		global $wpdb;

		$current_ids = $gallery_ids;
		if ( $thumb_id > 0 ) {
			$current_ids[] = $thumb_id;
		}
		$current_ids = array_unique( array_filter( $current_ids ) );

		if ( empty( $current_ids ) ) {
			// No images left for this product — soft-delete all its rows
			$wpdb->query( $wpdb->prepare(
				"UPDATE " . BDSK_DB::media_index_table() . "
				 SET status = 'deleted', index_updated_at = %s
				 WHERE product_id = %d AND image_type IN ('main','gallery') AND status = 'active'",
				current_time( 'mysql', true ),
				$product_id
			) );
			return;
		}

		$placeholders = implode( ',', array_fill( 0, count( $current_ids ), '%d' ) );
		$args         = array_merge( [ current_time( 'mysql', true ), $product_id ], $current_ids );

		$wpdb->query( $wpdb->prepare(
			"UPDATE " . BDSK_DB::media_index_table() . "
			 SET status = 'deleted', index_updated_at = %s
			 WHERE product_id = %d
			 AND image_type IN ('main','gallery')
			 AND status = 'active'
			 AND attachment_id NOT IN ({$placeholders})",
			...$args
		) );
	}

	// ---------------------------------------------------------------------------
	// Incremental hook: product / variation saved
	// ---------------------------------------------------------------------------

	public static function handle_save_product( int $post_id ): void {
		if ( wp_is_post_revision( $post_id ) || wp_is_post_autosave( $post_id ) ) {
			return;
		}

		$post_type = get_post_type( $post_id );

		if ( 'product' === $post_type ) {
			$thumb_id    = (int) get_post_meta( $post_id, '_thumbnail_id', true );
			$gallery_raw = get_post_meta( $post_id, '_product_image_gallery', true );
			$gallery_ids = array_filter( array_map( 'intval', $gallery_raw ? explode( ',', $gallery_raw ) : [] ) );

			if ( $thumb_id > 0 ) {
				self::upsert( $thumb_id, $post_id, 0, 'main' );
			}
			foreach ( $gallery_ids as $att_id ) {
				self::upsert( $att_id, $post_id, 0, 'gallery' );
			}

			self::prune_product_rows( $post_id, $thumb_id, $gallery_ids );

		} elseif ( 'product_variation' === $post_type ) {
			$thumb_id = (int) get_post_meta( $post_id, '_thumbnail_id', true );
			if ( $thumb_id > 0 ) {
				self::upsert( $thumb_id, $post_id, 0, 'variation' );
			}
		}
	}

	// ---------------------------------------------------------------------------
	// Incremental hook: attachment added or edited
	// ---------------------------------------------------------------------------

	public static function handle_attachment_change( int $attachment_id ): void {
		global $wpdb;

		// Refresh metadata on all existing active rows for this attachment
		$post = get_post( $attachment_id );
		if ( ! $post || 'attachment' !== $post->post_type ) {
			return;
		}

		$existing_rows = $wpdb->get_results( $wpdb->prepare(
			"SELECT product_id, order_id, image_type FROM " . BDSK_DB::media_index_table() . "
			 WHERE attachment_id = %d AND status = 'active'",
			$attachment_id
		) );

		foreach ( $existing_rows as $row ) {
			self::upsert( $attachment_id, (int) $row->product_id, (int) $row->order_id, $row->image_type );
		}
	}

	// ---------------------------------------------------------------------------
	// Incremental hook: attachment deleted
	// ---------------------------------------------------------------------------

	public static function handle_delete_attachment( int $attachment_id ): void {
		global $wpdb;

		$wpdb->query( $wpdb->prepare(
			"UPDATE " . BDSK_DB::media_index_table() . "
			 SET status = 'deleted', index_updated_at = %s
			 WHERE attachment_id = %d",
			current_time( 'mysql', true ),
			$attachment_id
		) );
	}

	// ---------------------------------------------------------------------------
	// Incremental hook: HPOS order saved
	// ---------------------------------------------------------------------------

	public static function handle_order_save( $order ): void {
		$meta_keys_raw = trim( BDSK_Settings::get( 'evidence_meta_keys', '' ) );
		if ( '' === $meta_keys_raw || ! BDSK_Settings::get( 'include_evidence_images', true ) ) {
			return;
		}

		if ( ! method_exists( $order, 'get_id' ) ) {
			return;
		}

		$order_id  = (int) $order->get_id();
		$meta_keys = array_filter( array_map( 'trim', explode( ',', $meta_keys_raw ) ) );

		$active_att_ids = [];

		foreach ( $meta_keys as $key ) {
			$val = $order->get_meta( $key );
			if ( $val && ctype_digit( (string) $val ) ) {
				$att_id = (int) $val;
				$post   = get_post( $att_id );
				if ( $post && 'attachment' === $post->post_type ) {
					self::upsert( $att_id, 0, $order_id, 'evidence' );
					$active_att_ids[] = $att_id;
				}
			}
		}

		// Soft-delete evidence rows for this order that are no longer present
		if ( ! empty( $active_att_ids ) ) {
			global $wpdb;
			$placeholders = implode( ',', array_fill( 0, count( $active_att_ids ), '%d' ) );
			$args         = array_merge( [ current_time( 'mysql', true ), $order_id ], $active_att_ids );
			$wpdb->query( $wpdb->prepare(
				"UPDATE " . BDSK_DB::media_index_table() . "
				 SET status = 'deleted', index_updated_at = %s
				 WHERE order_id = %d AND image_type = 'evidence'
				 AND status = 'active' AND attachment_id NOT IN ({$placeholders})",
				...$args
			) );
		} else {
			global $wpdb;
			$wpdb->query( $wpdb->prepare(
				"UPDATE " . BDSK_DB::media_index_table() . "
				 SET status = 'deleted', index_updated_at = %s
				 WHERE order_id = %d AND image_type = 'evidence' AND status = 'active'",
				current_time( 'mysql', true ),
				$order_id
			) );
		}
	}

	// ---------------------------------------------------------------------------
	// Cleanup: prune soft-deleted rows older than 30 days
	// ---------------------------------------------------------------------------

	public static function prune_old_deleted_rows(): int {
		global $wpdb;

		$deleted = $wpdb->query( $wpdb->prepare(
			"DELETE FROM " . BDSK_DB::media_index_table() . "
			 WHERE status = 'deleted' AND index_updated_at < DATE_SUB(%s, INTERVAL 30 DAY)",
			current_time( 'mysql', true )
		) );
		return (int) $deleted;
	}

	// ---------------------------------------------------------------------------
	// HPOS detection
	// ---------------------------------------------------------------------------

	private static function is_hpos_active(): bool {
		return 'yes' === get_option( 'woocommerce_custom_orders_table_enabled', 'no' );
	}
}
