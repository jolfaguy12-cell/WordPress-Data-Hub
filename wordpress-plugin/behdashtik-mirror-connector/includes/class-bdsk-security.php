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
		'enabled'                    => false,
		'read_access_enabled'        => false,
		'backup_export_enabled'      => false,
		'allowed_ips'                => '',
		'disable_ip_check'           => false,
		'api_key_hash'               => '',   // legacy: hash-only fallback
		'debug_log_enabled'          => false,
		'media_manifest_enabled'     => true,
		'index_unknown_media'        => false,
		'include_evidence_images'    => true,
		'evidence_meta_keys'         => '',
		'event_sync_enabled'         => true,
		'rate_limit_enabled'         => true,
		'rate_limit_max_failures'    => 10,
		'rate_limit_window_minutes'  => 15,
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

	/** wp_option key for the AES-256-CBC encrypted secret blob. */
	private const ENC_OPTION = 'bdsk_api_secret_enc';

	// -----------------------------------------------------------------------
	// Encryption helpers (private)
	// -----------------------------------------------------------------------

	private static function derive_enc_key(): string {
		return hash_hmac( 'sha256', 'bdsk|v1', wp_salt( 'auth' ), true );
	}

	private static function encrypt_secret( string $plaintext ): string {
		$key        = self::derive_enc_key();
		$iv         = openssl_random_pseudo_bytes( 16 );
		$ciphertext = openssl_encrypt( $plaintext, 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv );
		return base64_encode( $iv . $ciphertext );
	}

	private static function decrypt_secret( string $blob ): string|false {
		$raw = base64_decode( $blob, true );
		if ( false === $raw || strlen( $raw ) <= 16 ) {
			return false;
		}
		$iv         = substr( $raw, 0, 16 );
		$ciphertext = substr( $raw, 16 );
		$key        = self::derive_enc_key();
		$plaintext  = openssl_decrypt( $ciphertext, 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv );
		return ( false !== $plaintext && '' !== $plaintext ) ? $plaintext : false;
	}

	// -----------------------------------------------------------------------
	// API secret — public interface
	// -----------------------------------------------------------------------

	public static function get_api_secret(): ?string {
		if ( defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET ) {
			return BDSK_API_SECRET;
		}

		$blob = get_option( self::ENC_OPTION, '' );
		if ( '' !== $blob ) {
			$plaintext = self::decrypt_secret( $blob );
			if ( false !== $plaintext ) {
				return $plaintext;
			}
		}

		return null;
	}

	public static function has_secret(): bool {
		if ( defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET ) {
			return true;
		}
		if ( '' !== get_option( self::ENC_OPTION, '' ) ) {
			return true;
		}
		if ( '' !== BDSK_Settings::get( 'api_key_hash', '' ) ) {
			return true;
		}
		return false;
	}

	public static function using_constant(): bool {
		return defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET;
	}

	public static function using_encrypted_option(): bool {
		return ! self::using_constant() && '' !== get_option( self::ENC_OPTION, '' );
	}

	public static function generate_and_store(): string {
		$plaintext = bin2hex( openssl_random_pseudo_bytes( 32 ) );
		$blob      = self::encrypt_secret( $plaintext );
		update_option( self::ENC_OPTION, $blob, false );
		return $plaintext;
	}

	// -----------------------------------------------------------------------
	// API key validation
	// -----------------------------------------------------------------------

	public static function validate_api_key( string $provided_key ): bool {
		if ( '' === $provided_key ) {
			return false;
		}

		if ( self::using_constant() ) {
			return hash_equals( BDSK_API_SECRET, $provided_key );
		}

		$blob = get_option( self::ENC_OPTION, '' );
		if ( '' !== $blob ) {
			$plaintext = self::decrypt_secret( $blob );
			if ( false !== $plaintext ) {
				return hash_equals( $plaintext, $provided_key );
			}
		}

		$stored_hash = BDSK_Settings::get( 'api_key_hash', '' );
		if ( '' !== $stored_hash ) {
			return hash_equals( $stored_hash, hash( 'sha256', $provided_key ) );
		}

		return false;
	}

	// -----------------------------------------------------------------------
	// IP validation
	// -----------------------------------------------------------------------

	public static function get_request_ip( WP_REST_Request $request ): string {
		// phpcs:ignore WordPress.Security.ValidatedSanitizedInput
		$remote = $_SERVER['REMOTE_ADDR'] ?? '';

		if ( '' !== $remote ) {
			return sanitize_text_field( $remote );
		}

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
			return false;
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

	public static function validate_request( WP_REST_Request $request ): true|WP_Error {
		$start    = microtime( true );
		$endpoint = $request->get_route();
		$ip       = self::get_request_ip( $request );
		$ip_hash  = hash( 'sha256', $ip );
		$fail_key = 'bdsk_authfail_' . $ip_hash;

		$reject = static function ( string $reason, int $http_status = 403 ) use ( $endpoint, $ip, $start ): WP_Error {
			$duration_ms = (int) round( ( microtime( true ) - $start ) * 1000 );
			BDSK_DB::log_request( $endpoint, $ip, 'rejected', $reason, $duration_ms );
			BDSK_Stats::increment( $endpoint, 'rejected', $reason );
			bdsk_log( "Request rejected: {$reason}", [ 'endpoint' => $endpoint, 'ip' => $ip ] );
			return new WP_Error( $reason, 'Request rejected.', [ 'status' => $http_status ] );
		};

		// Rate limit — check before anything else to bail cheaply on brute-force
		if ( BDSK_Settings::get( 'rate_limit_enabled', true ) ) {
			$max_failures = (int) BDSK_Settings::get( 'rate_limit_max_failures', 10 );
			$fail_count   = (int) ( get_transient( $fail_key ) ?: 0 );
			if ( $fail_count >= $max_failures ) {
				$duration_ms = (int) round( ( microtime( true ) - $start ) * 1000 );
				BDSK_DB::log_request( $endpoint, $ip, 'rejected', 'rate_limited', $duration_ms );
				BDSK_Stats::increment( $endpoint, 'rejected', 'rate_limited' );
				bdsk_log( "Rate limited: {$ip}", [ 'endpoint' => $endpoint, 'fail_count' => $fail_count ] );
				return new WP_Error( 'rate_limited', 'Too many authentication failures.', [ 'status' => 429 ] );
			}
		}

		// 1. Connector enabled?
		if ( ! BDSK_Settings::get( 'enabled' ) ) {
			return $reject( 'connector_disabled' );
		}

		// 2. Read access enabled?
		if ( ! BDSK_Settings::get( 'read_access_enabled' ) ) {
			return $reject( 'read_access_disabled' );
		}

		// 3. Authorization header — Bearer token (also accepts X-BDSK-Key)
		$auth = $request->get_header( 'Authorization' );
		if ( ! $auth ) {
			$auth = $request->get_header( 'X-BDSK-Key' );
			$key  = $auth ? trim( $auth ) : '';
		} else {
			$key = trim( str_replace( 'Bearer ', '', $auth ) );
		}

		if ( '' === $key || ! self::validate_api_key( $key ) ) {
			// Increment fail counter; counter persists for the configured window
			$window     = (int) BDSK_Settings::get( 'rate_limit_window_minutes', 15 );
			$fail_count = (int) ( get_transient( $fail_key ) ?: 0 );
			set_transient( $fail_key, $fail_count + 1, $window * 60 );
			return $reject( 'bad_key' );
		}

		// 4. IP check
		if ( ! self::validate_ip( $ip ) ) {
			return $reject( 'ip_mismatch' );
		}

		// Success — reset fail counter and log
		delete_transient( $fail_key );
		$duration_ms = (int) round( ( microtime( true ) - $start ) * 1000 );
		BDSK_DB::log_request( $endpoint, $ip, 'accepted', null, $duration_ms );
		BDSK_Stats::increment( $endpoint, 'accepted' );

		return true;
	}
}
