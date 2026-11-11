package com.echopersona.echo

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import kotlinx.coroutines.flow.MutableStateFlow

/** Network discovery: finds "Echo on …" on the home Wi-Fi (the Mac announces `_echolink._tcp` with dns-sd). */
class Discovery(private val ctx: Context) {
    data class Found(val name: String, val host: String, val port: Int, val macId: String)

    val found = MutableStateFlow<List<Found>>(emptyList())
    private val nsd = ctx.getSystemService(NsdManager::class.java)
    private var lock: WifiManager.MulticastLock? = null
    private var listener: NsdManager.DiscoveryListener? = null

    fun start() {
        if (listener != null) return
        lock = ctx.applicationContext.getSystemService(WifiManager::class.java)
            .createMulticastLock("echo-discovery").apply { setReferenceCounted(false); acquire() }
        val l = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onServiceLost(info: NsdServiceInfo) {
                found.value = found.value.filter { it.name != info.serviceName }
            }
            override fun onServiceFound(info: NsdServiceInfo) {
                @Suppress("DEPRECATION")
                nsd.resolveService(info, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(i: NsdServiceInfo, errorCode: Int) {}
                    override fun onServiceResolved(i: NsdServiceInfo) {
                        @Suppress("DEPRECATION") val host = i.host?.hostAddress ?: return
                        if (host.contains(":")) return                  // IPv4 home address only
                        val id = i.attributes["id"]?.let { String(it) } ?: ""
                        val f = Found(i.serviceName, host, i.port, id)
                        found.value = (found.value.filter { it.macId != id || id.isEmpty() && it.name != f.name } + f)
                    }
                })
            }
        }
        listener = l
        try { nsd.discoverServices("_echolink._tcp", NsdManager.PROTOCOL_DNS_SD, l) } catch (_: Exception) {}
    }

    fun stop() {
        listener?.let { try { nsd.stopServiceDiscovery(it) } catch (_: Exception) {} }
        listener = null
        lock?.release(); lock = null
    }
}
