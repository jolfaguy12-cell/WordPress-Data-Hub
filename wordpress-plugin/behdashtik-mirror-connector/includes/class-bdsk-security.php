<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

// ---------------------------------------------------------------------------
// Settings helper — thin wrapper around wp_options
// ---------------------------------------------------------------------------

class BDSK_Settings {

	private const OPTION_KEY = 'bdsk_settings';

	private static array $defaults = [
		'enabled'                => false,
		'read_access_enabled'    => false,
		'backup_export_enabled'  => false,
		'allowed_ips'            => '',
		'disable_ip_check'       => false,
		'api_key_hash'           => '',   // only used when BDSK_API_SECRET is not defined
		'debug_log_enabled'      => false,
		'last_successful_request' => '',
		'last_failed_request'    => '',
		'last_export_downloaded' => '',
	];

	public static function get( string $key, mixed $default = null ): mixed {
		$settings = get_option( self::OPTION_KEY, [] );
		if ( isset( $settings[ $key ] ) ) {
			return $settings[ $key ];
		}
		return $default ?? self::$defaults[ $key ] ?? null;
	}

	public static function set( string $key, mixed $value ): void {
		$settings         = get_option( self::OPTION_KEY, [] );
		$settings[ $key ] = $value;
		update_option( self::OPTION_KEY, $settings, false );
	}

	public static function all(): array {
		return array_merge( self::$defaults, get_option( self::OPTION_KEY, [] ) );
	}

	public static function bulk_update( array $data ): void {
		$settings = array_merge( get_option( self::OPTION_KEY, [] ), $data );
		update_option( self::OPTION_KEY, $settings, false );
	}
}

// ---------------------------------------------------------------------------
// Security / request validation middleware
// ---------------------------------------------------------------------------

class BDSK_Security {

	// -----------------------------------------------------------------------
	// API secret
	// -----------------------------------------------------------------------

	/**
	 * Returns the plaintext API secret.
	 * Priority: PHP constant BDSK_API_SECRET > stored plaintext (never stored here;
	 * only the hash is stored, so we cannot return it in fallback mode).
	 * Returns null when no constant is defined and we cannot verify in hash-only mode.
	 */
	public static function get_api_secret(): ?string {
		if ( defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET ) {
			return BDSK_API_SECRET;
		}
		return null; // hash-only fallback; validated via hash_equals below
	}

	public static function using_constant(): bool {
		return defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET;
	}

	/**
	 * Constant-time key comparison.
	 * Works in both constant-in-php-config mode (compare against plaintext)
	 * and hash-stored fallback mode (compare hash of provided key).
	 */
	public static function validate_api_key( string $provided_key ): bool {
		if ( '' === $provided_key ) {
			return false;
		}

		if ( self::using_constant() ) {
			return hash_equals( BDSK_API_SECRET, $provided_key );
		}

		// Fallback: compare sha256 of provided key against stored hash
		$stored_hash = BDSK_Settings::get( 'api_key_hash', '' );
		if ( '' === $stored_hash ) {
			return false;
		}
		return hash_equals( $stored_hash, hash( 'sha256', $provided_key ) );
	}

	// -----------------------------------------------------------------------
	// IP validation
	// -----------------------------------------------------------------------

	/**
	 * Returns the requester's IP.
	 *
	 * Note: X-Forwarded-For is checked only as a fallback when REMOTE_ADDR is
	 * missing or empty. XFF is spoofable unless your reverse proxy is configured
	 * to overwrite (not append) it — enable "disable_ip_check" in local dev if
	 * your proxy setup makes this unreliable.
	 */
	public static function get_request_ip( WP_REST_Request $request ): string {
		// phpcs:ignore WordPress.Security.ValidatedSanitizedInput
		$remote = $_SERVER['REMOTE_ADDR'] ?? '';

		if ( '' !== $remote ) {
			return sanitize_text_field( $remote );
		}

		// Fallback: first IP in X-Forwarded-For (may be set by load balancer / Cloudflare)
		$xff = $request->get_header( 'X-Forwarded-For' );
		if ( $xff ) {
			$parts = explode( ',', $xff );
			return trim( sanitize_text_field( $parts[0] ) );
		}

		return '';
	}

	public static function validate_ip( string $ip ): bool {
		if ( BDSK_Settings::get( 'disable_ip_check' ) ) {
			return true;
		}

		$allowed_raw = BDSK_Settings::get( 'allowed_ips', '' );
		if ( '' === $allowed_raw ) {
			return false; // no IPs configured = deny all
		}

		$allowed = array_filter( array_map( 'trim', explode( ',', $allowed_raw ) ) );

		foreach ( $allowed as $allowed_ip ) {
			if ( $ip === $allowed_ip ) {
				return true;
			}
		}

		return false;
	}

	// -----------------------------------------------------------------------
	// Main request validation middleware
	// -----------------------------------------------------------------------

	/**
	 * Returns true on success, WP_Error on failure.
	 * Logs every request (accepted and rejected) to bdsk_request_log.
	 */
	public static function validate_request( WP_REST_Request $request ): true|WP_Error {
		$start    = microtime( true );
		$endpoint = $request->get_route();
		$ip       = self::get_request_ip( $request );

		$reject = static function ( string $reason ) use ( $endpoint, $ip, $start ): WP_Error {
			$duration_ms = (int) round( ( microtime( true ) - $start ) * 1000 );
			BDSK_DB::log_request( $endpoint, $ip, 'rejected', $reason, $duration_ms );
			BDSK_Settings::set( 'last_failed_request', current_time( 'mysql', true ) );
			bdsk_log( "Request rejected: {$reason}", [ 'endpoint' => $endpoint, 'ip' => $ip ] );
			return new WP_Error( $reason, 'Request rejected.', [ 'status' => 403 ] );
		};

		// 1. Connector enabled?
		if ( ! BDSK_Settings::get( 'enabled' ) ) {
			return $reject( 'connector_disabled' );
		}

		// 2. Read access enabled?
		if ( ! BDSK_Settings::get( 'read_access_enabled' ) ) {
			return $reject( 'read_access_disabled' );
		}

		// 3. Authorization header — Bearer token
		$auth = $request->get_header( 'Authorization' );
		if ( ! $auth ) {
			// Also accept X-BDSK-Key for simpler clients
			$auth = $request->get_header( 'X-BDSK-Key' );
			$key  = $auth ? trim( $auth ) : '';
		} else {
			$key = trim( str_replace( 'Bearer ', '', $auth ) );
		}

		if ( '' === $key || ! self::validate_api_key( $key ) ) {
			return $reject( 'bad_key' );
		}

		// 4. IP check
		if ( ! self::validate_ip( $ip ) ) {
			return $reject( 'ip_mismatch' );
		}

		// 5. Log accepted request
		$duration_ms = (int) round( ( microtime( true ) - $start ) * 1000 );
		BDSK_DB::log_request( $endpoint, $ip, 'accepted', null, $duration_ms );
		BDSK_Settings::set( 'last_successful_request', current_time( 'mysql', true ) );

		return true;
	}
}
