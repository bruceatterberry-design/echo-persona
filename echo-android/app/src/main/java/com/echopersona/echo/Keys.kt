package com.echopersona.echo

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import android.util.Base64
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/** This device's own key, made inside Android Keystore. It can't be copied off the device; the Mac keeps only the
 *  public half and checks a fresh signature at every sign-in (echolink/link.py). */
object Keys {
    private const val ALIAS = "echo-link-device"

    private fun store() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    fun ensure() {
        if (store().containsAlias(ALIAS)) return
        fun gen(strongBox: Boolean) {
            val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .apply { if (strongBox) setIsStrongBoxBacked(true) }
                .build()
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").apply { initialize(spec) }
                .generateKeyPair()
        }
        try { gen(true) } catch (e: StrongBoxUnavailableException) { gen(false) } catch (e: Exception) { gen(false) }
    }

    /** Remove and make a new key (used when re-linking after the Mac removed this device). */
    fun reset() { store().deleteEntry(ALIAS); ensure() }

    fun publicKey(): String { ensure(); return b64(store().getCertificate(ALIAS).publicKey.encoded) }

    fun sign(msg: String): String {
        ensure()
        val key = store().getKey(ALIAS, null) as PrivateKey
        return b64(Signature.getInstance("SHA256withECDSA").run { initSign(key); update(msg.toByteArray()); sign() })
    }
}

fun b64(b: ByteArray): String = Base64.encodeToString(b, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
fun unb64(s: String): ByteArray = Base64.decode(s, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
fun sha256(b: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(b)
fun randomBytes(n: Int) = ByteArray(n).also { SecureRandom().nextBytes(it) }

/** The 6-digit number both screens show (same formula as echolink/link.py pair_code). */
fun pairCode(fp: ByteArray, phoneNonce: ByteArray, macNonce: ByteArray): String {
    val h = sha256("echolink-code-v1".toByteArray() + fp + phoneNonce + macNonce)
    val n = ((h[0].toLong() and 0xff) shl 24) or ((h[1].toLong() and 0xff) shl 16) or
            ((h[2].toLong() and 0xff) shl 8) or (h[3].toLong() and 0xff)
    return "%06d".format(n % 1_000_000)
}

/** What this device remembers about its link (none of it secret: the private key stays in Keystore). */
data class Linked(
    val deviceId: String, val user: String, val macId: String, val macName: String, val pin: String,
    val lan: List<String>, val port: Int, val ts: String, val ti: String,
)

object Saved {
    private fun prefs(c: Context) = c.getSharedPreferences("echo-link", Context.MODE_PRIVATE)

    fun load(c: Context): Linked? {
        val p = prefs(c)
        val id = p.getString("device_id", null) ?: return null
        return Linked(id, p.getString("user", "")!!, p.getString("mac_id", "")!!, p.getString("mac_name", "Echo")!!,
            p.getString("pin", "")!!, p.getString("lan", "")!!.split(",").filter { it.isNotBlank() },
            p.getInt("port", 8771), p.getString("ts", "")!!, p.getString("ti", "")!!)
    }

    fun save(c: Context, l: Linked) = prefs(c).edit()
        .putString("device_id", l.deviceId).putString("user", l.user).putString("mac_id", l.macId)
        .putString("mac_name", l.macName).putString("pin", l.pin).putString("lan", l.lan.joinToString(","))
        .putInt("port", l.port).putString("ts", l.ts).putString("ti", l.ti).apply()

    /** Newer home addresses the Mac reports (DHCP may change them); the pin never changes this way. */
    fun updateRoutes(c: Context, lan: List<String>, ts: String, ti: String) {
        val e = prefs(c).edit()
        if (lan.isNotEmpty()) e.putString("lan", lan.joinToString(","))
        if (ts.isNotBlank()) e.putString("ts", ts)
        if (ti.isNotBlank()) e.putString("ti", ti)
        e.apply()
    }

    fun forget(c: Context) = prefs(c).edit().clear().apply()
}
