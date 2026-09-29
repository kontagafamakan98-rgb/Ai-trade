package com.aitrade.data

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Chiffrement symétrique des identifiants broker (clé API / secret) avant
 * stockage en base locale.
 *
 * La clé AES est générée et conservée dans l'Android Keystore (jamais exposée
 * dans l'application ni sauvegardée en clair). Format stocké : préfixe "enc:"
 * suivi de Base64(IV || ciphertext). Les valeurs déjà chiffrées ne sont pas
 * rechiffrées, et les anciennes valeurs en clair restent lisibles (migration
 * douce).
 */
object Crypto {
    private const val KEY_ALIAS = "ai_trade_creds_key"
    private const val ANDROID_KEYSTORE = "AndroidKeyStore"
    private const val TRANSFORMATION = "AES/GCM/NoPadding"
    private const val IV_LENGTH = 12
    private const val TAG_LENGTH_BITS = 128
    private const val PREFIX = "enc:"

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let {
            return it.secretKey
        }

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec
                .Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    /** Chiffre une chaîne. Une chaîne vide/vierge reste vide. */
    fun encrypt(plain: String): String {
        if (plain.isBlank() || plain.startsWith(PREFIX)) return plain
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
            val iv = cipher.iv
            val encrypted = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
            PREFIX + Base64.encodeToString(iv + encrypted, Base64.NO_WRAP)
        } catch (e: Exception) {
            // En cas d'échec du Keystore, on n'écrit JAMAIS en clair : on
            // renvoie une chaîne vide pour éviter toute fuite.
            ""
        }
    }

    /** Déchiffre une valeur stockée. Supporte les anciennes valeurs en clair. */
    fun decrypt(stored: String): String {
        if (stored.isBlank()) return ""
        if (!stored.startsWith(PREFIX)) {
            // Valeur héritée en clair (avant migration) : on la renvoie telle
            // quelle ; elle sera chiffrée au prochain enregistrement.
            return stored
        }
        return try {
            val combined = Base64.decode(stored.removePrefix(PREFIX), Base64.NO_WRAP)
            val iv = combined.copyOfRange(0, IV_LENGTH)
            val data = combined.copyOfRange(IV_LENGTH, combined.size)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(TAG_LENGTH_BITS, iv))
            String(cipher.doFinal(data), Charsets.UTF_8)
        } catch (e: Exception) {
            ""
        }
    }

    /** Indique si une valeur stockée est chiffrée. */
    fun isEncrypted(stored: String): Boolean = stored.startsWith(PREFIX)
}
