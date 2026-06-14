<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Export_Rest {

	private const NAMESPACE = 'behdashtik-connector/v1';

	public static function init(): void {
		add_action( 'rest_api_init', [ __CLASS__, 'register_routes' ] );
		// Discard any output (PHP notices/warnings) that accumulated before the REST
		// response is sent. This prevents stray text from corrupting JSON or binary downloads.
		add_filter( 'rest_pre_serve_request', static function ( $served ) {
			while ( ob_get_level() ) {
				ob_end_clean();
			}
			return $served;
		}, 1 );
	}

	public static function register_routes(): void {
		// Health — lightest route, registered first
		register_rest_route( self::NAMESPACE, '/health', [
			'methods'             => WP_REST_Server::READABLE,
			'callback'            => [ __CLASS__, 'handle_health' ],
			'permission_callback' => '__return_true', // auth handled inside
		] );

		register_rest_route( self::NAMESPACE, '/db-export/start', [
			'methods'             => WP_REST_Server::CREATABLE,
			'callback'            => [ __CLASS__, 'handle_start' ],
			'permission_callback' => '__return_true',
		] );

		register_rest_route( self::NAMESPACE, '/db-export/status/(?P<job_id>[0-9a-f\-]{36})', [
			'methods'             => WP_REST_Server::READABLE,
			'callback'            => [ __CLASS__, 'handle_status' ],
			'permission_callback' => '__return_true',
			'args'                => [
				'job_id' => [
					'validate_callback' => fn( $v ) => (bool) preg_match( '/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i', $v ),
				],
			],
		] );

		register_rest_route( self::NAMESPACE, '/db-export/download/(?P<job_id>[0-9a-f\-]{36})', [
			'methods'             => WP_REST_Server::READABLE,
			'callback'            => [ __CLASS__, 'handle_download' ],
			'permission_callback' => '__return_true',
			'args'                => [
				'job_id' => [
					'validate_callback' => fn( $v ) => (bool) preg_match( '/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i', $v ),
				],
				'part'  => [
					'default'           => 1,
					'sanitize_callback' => 'absint',
					'validate_callback' => fn( $v ) => (int) $v >= 1 && (int) $v <= 999,
				],
				'token' => [
					'required'          => true,
					'sanitize_callback' => 'sanitize_text_field',
				],
			],
		] );

		register_rest_route( self::NAMESPACE, '/db-export/confirm-download', [
			'methods'             => WP_REST_Server::CREATABLE,
			'callback'            => [ __CLASS__, 'handle_confirm_download' ],
			'permission_callback' => '__return_true',
		] );
	}

	// ---------------------------------------------------------------------------
	// GET /health
	// ---------------------------------------------------------------------------

	public static function handle_health( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}
		return new WP_REST_Response( BDSK_Health::get_data(), 200 );
	}

	// ---------------------------------------------------------------------------
	// POST /db-export/start
	// ---------------------------------------------------------------------------

	public static function handle_start( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}

		// test mode: only allowed when constant is explicitly set
		$test_mode = false;
		if ( defined( 'BDSK_ALLOW_TEST_EXPORT' ) && BDSK_ALLOW_TEST_EXPORT ) {
			$test_mode = (bool) $request->get_param( 'test' );
		}

		$result = BDSK_Export_Job::create( $test_mode );
		if ( is_wp_error( $result ) ) {
			// 409 with existing job_id is a valid "already running" response
			$data = $result->get_error_data();
			if ( isset( $data['job_id'] ) ) {
				return new WP_REST_Response( [
					'error'  => $result->get_error_code(),
					'job_id' => $data['job_id'],
					'status' => 'running',
				], 409 );
			}
			return $result;
		}

		return new WP_REST_Response( $result, 202 );
	}

	// ---------------------------------------------------------------------------
	// GET /db-export/status/{job_id}
	// ---------------------------------------------------------------------------

	public static function handle_status( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}

		$job_id = $request->get_param( 'job_id' );
		$job    = BDSK_DB::get_job( $job_id );
		if ( ! $job ) {
			return new WP_Error( 'not_found', 'Job not found.', [ 'status' => 404 ] );
		}

		$is_ready = 'ready' === $job['status'];
		$manifest = $is_ready ? json_decode( $job['archive_manifest'] ?: '{}', true ) : null;

		$response = [
			'job_id'           => $job['job_id'],
			'status'           => $job['status'],
			'progress_percent' => (float) $job['progress_percent'],
			'current_table'    => $job['current_table'],
			'current_offset'   => (int) $job['current_offset'],
			'last_error'       => $job['last_error'],
			'file_ready'       => $is_ready,
			'created_at'       => $job['created_at'],
			'updated_at'       => $job['updated_at'],
		];

		if ( $is_ready && $manifest ) {
			// Include per-part sha256 so Server 2 can verify checksums after download.
			// Token is included here so Server 2 can build the download URL without
			// a separate round-trip — it is already gated behind the API secret.
			$response['archive_manifest'] = [
				'parts'           => $manifest['parts'] ?? [],  // filename, size, sha256
				'tables_included' => $manifest['tables_included'] ?? [],
				'db_prefix'       => $manifest['db_prefix'] ?? '',
			];
			$response['checksum']        = $job['checksum'];
			$response['archive_size']    = (int) $job['archive_size'];
			// Regenerate a fresh token (idempotent — same expiry window)
			$response['download_token']  = BDSK_Export_Job::generate_download_token( $job_id );
		}

		return new WP_REST_Response( $response, 200 );
	}

	// ---------------------------------------------------------------------------
	// GET /db-export/download/{job_id}?part=N&token=...
	// ---------------------------------------------------------------------------

	public static function handle_download( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}

		$job_id = $request->get_param( 'job_id' );
		$part   = (int) $request->get_param( 'part' );
		$token  = $request->get_param( 'token' );

		if ( ! BDSK_Export_Job::validate_download_token( $job_id, $token ) ) {
			return new WP_Error( 'invalid_token', 'Invalid or expired download token.', [ 'status' => 403 ] );
		}

		$job = BDSK_DB::get_job( $job_id );
		if ( ! $job ) {
			return new WP_Error( 'not_found', 'Job not found.', [ 'status' => 404 ] );
		}

		$part_path = BDSK_Export_Job::get_part_path( $job_id, $part );
		if ( ! file_exists( $part_path ) ) {
			return new WP_Error( 'part_not_found', "Part {$part} not found.", [ 'status' => 404 ] );
		}

		// Mark as downloading on first part request
		if ( 'ready' === $job['status'] ) {
			BDSK_DB::update_job( $job_id, [ 'status' => 'downloading' ] );
		}

		$filename = basename( $part_path );
		$filesize = filesize( $part_path );

		// Stream the file — do NOT load into memory
		header( 'Content-Type: application/gzip' );
		header( 'Content-Disposition: attachment; filename="' . $filename . '"' );
		header( 'Content-Length: ' . $filesize );
		header( 'Cache-Control: no-cache, no-store, must-revalidate' );
		header( 'X-Content-Type-Options: nosniff' );

		// Disable REST response output buffering and exit directly
		while ( ob_get_level() ) {
			ob_end_clean();
		}

		// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_readfile
		readfile( $part_path );
		exit;
	}

	// ---------------------------------------------------------------------------
	// POST /db-export/confirm-download
	// ---------------------------------------------------------------------------

	public static function handle_confirm_download( WP_REST_Request $request ): WP_REST_Response|WP_Error {
		$auth = BDSK_Security::validate_request( $request );
		if ( is_wp_error( $auth ) ) {
			return $auth;
		}

		$job_id = sanitize_text_field( $request->get_param( 'job_id' ) );
		if ( ! preg_match( '/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i', $job_id ) ) {
			return new WP_Error( 'invalid_job_id', 'Invalid job ID.', [ 'status' => 400 ] );
		}

		$job = BDSK_DB::get_job( $job_id );
		if ( ! $job ) {
			return new WP_Error( 'not_found', 'Job not found.', [ 'status' => 404 ] );
		}

		if ( ! in_array( $job['status'], [ 'downloading', 'ready' ], true ) ) {
			return new WP_Error( 'invalid_state', 'Job is not in a downloadable state.', [ 'status' => 409 ] );
		}

		// Invalidate token immediately
		BDSK_DB::update_job( $job_id, [
			'status'             => 'downloaded',
			'download_token_hash' => null,
		] );

		BDSK_Settings::set( 'last_export_downloaded', current_time( 'mysql', true ) );

		// Trigger immediate cleanup
		BDSK_Cleanup::cleanup_job( $job_id );

		bdsk_log( "Download confirmed for job {$job_id}." );

		return new WP_REST_Response( [ 'confirmed' => true, 'job_id' => $job_id ], 200 );
	}
}
