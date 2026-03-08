## ADDED Requirements

### Requirement: AES-256-GCM encryption module
The system SHALL provide an `AES` struct in `internal/crypto/aes.go` that encrypts and decrypts arbitrary byte data using AES-256-GCM. The encryption key SHALL be a 256-bit (32 byte) key provided at initialization.

#### Scenario: Encrypt and decrypt roundtrip
- **WHEN** data is encrypted with `Encrypt(plaintext)` and then decrypted with `Decrypt(ciphertext)`
- **THEN** the decrypted output SHALL equal the original plaintext

#### Scenario: Random nonce per encryption
- **WHEN** the same plaintext is encrypted twice
- **THEN** the two ciphertexts SHALL be different (due to random nonce)

#### Scenario: Tampered ciphertext detection
- **WHEN** a ciphertext is modified and then decrypted
- **THEN** `Decrypt` SHALL return an error (GCM authentication failure)

### Requirement: Ciphertext format
The ciphertext output SHALL be formatted as `nonce || gcm_ciphertext` where the nonce is the standard GCM nonce size (12 bytes) prepended to the authenticated ciphertext.

#### Scenario: Ciphertext structure
- **WHEN** `Encrypt` produces a ciphertext
- **THEN** the first 12 bytes SHALL be the nonce, and the remaining bytes SHALL be the GCM-sealed ciphertext

### Requirement: Invalid key rejection
The `NewAES` constructor SHALL reject keys that are not exactly 32 bytes.

#### Scenario: Wrong key size
- **WHEN** `NewAES` is called with a key shorter or longer than 32 bytes
- **THEN** it SHALL return an error
