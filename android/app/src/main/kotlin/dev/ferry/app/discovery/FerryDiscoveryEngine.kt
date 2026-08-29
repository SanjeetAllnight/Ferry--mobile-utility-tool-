package dev.ferry.app.discovery

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.util.Log
import dev.ferry.app.protocol.ProtocolConstants
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue

class FerryDiscoveryEngine(private val context: Context) {

    companion object {
        private const val TAG = "FerryDiscovery"
        private const val SERVICE_TYPE = "_ferry._tcp"
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val nsdManager = context.getSystemService(Context.NSD_SERVICE) as? NsdManager
    private val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
    private var multicastLock: WifiManager.MulticastLock? = null

    val deviceId: String by lazy {
        val prefs = context.getSharedPreferences("ferry_prefs", Context.MODE_PRIVATE)
        val existing = prefs.getString("device_id", null)
        if (existing != null) {
            existing
        } else {
            val generated = UUID.randomUUID().toString()
            prefs.edit().putString("device_id", generated).apply()
            generated
        }
    }

    val deviceName: String by lazy {
        val model = Build.MODEL ?: "Android Device"
        "$model (Ferry)"
    }

    private val _discoveredDevices = MutableStateFlow<List<DiscoveredDevice>>(emptyList())
    val discoveredDevices: StateFlow<List<DiscoveredDevice>> = _discoveredDevices.asStateFlow()

    private val deviceMap = ConcurrentHashMap<String, DiscoveredDevice>() // deviceId -> DiscoveredDevice
    private val serviceNameToDeviceId = ConcurrentHashMap<String, String>() // serviceName -> deviceId

    private var isRegistered = false
    private var isDiscovering = false
    private var registrationListener: NsdManager.RegistrationListener? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null

    // Queue to prevent concurrent resolve calls on older Android platforms
    private val resolveQueue = ConcurrentLinkedQueue<NsdServiceInfo>()
    private var isResolving = false

    @Synchronized
    fun start() {
        if (nsdManager == null) {
            Log.e(TAG, "NsdManager is not available on this device.")
            return
        }

        acquireMulticastLock()
        registerService()
        discoverServices()
    }

    @Synchronized
    fun stop() {
        if (nsdManager == null) return

        stopDiscovery()
        unregisterService()
        releaseMulticastLock()

        deviceMap.clear()
        serviceNameToDeviceId.clear()
        _discoveredDevices.value = emptyList()
        Log.i(TAG, "Ferry discovery engine stopped.")
    }

    private fun acquireMulticastLock() {
        try {
            if (multicastLock == null) {
                multicastLock = wifiManager?.createMulticastLock("ferry_nsd_lock")?.apply {
                    setReferenceCounted(true)
                }
            }
            multicastLock?.acquire()
            Log.d(TAG, "MulticastLock acquired.")
        } catch (e: Exception) {
            Log.w(TAG, "Could not acquire MulticastLock: ${e.message}")
        }
    }

    private fun releaseMulticastLock() {
        try {
            if (multicastLock?.isHeld == true) {
                multicastLock?.release()
                Log.d(TAG, "MulticastLock released.")
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not release MulticastLock: ${e.message}")
        }
    }

    private fun registerService() {
        if (isRegistered || nsdManager == null) return

        val serviceInfo = NsdServiceInfo().apply {
            serviceType = SERVICE_TYPE
            serviceName = "Ferry-${deviceId.take(8)}"
            port = ProtocolConstants.DEFAULT_PORT

            setAttribute("v", "${ProtocolConstants.PROTOCOL_VERSION}")
            setAttribute("id", deviceId)
            setAttribute("name", deviceName)
            setAttribute("type", "mobile")
            setAttribute("os", "android")
            setAttribute("port", "${ProtocolConstants.DEFAULT_PORT}")
            setAttribute("app_version", "0.1.0")
        }

        registrationListener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(registeredInfo: NsdServiceInfo) {
                isRegistered = true
                Log.i(TAG, "Ferry service successfully registered as: ${registeredInfo.serviceName}")
            }

            override fun onRegistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                isRegistered = false
                Log.e(TAG, "Ferry service registration failed with code: $errorCode")
            }

            override fun onServiceUnregistered(serviceInfo: NsdServiceInfo) {
                isRegistered = false
                Log.i(TAG, "Ferry service unregistered: ${serviceInfo.serviceName}")
            }

            override fun onUnregistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                Log.w(TAG, "Ferry service unregistration failed with code: $errorCode")
            }
        }

        try {
            nsdManager.registerService(serviceInfo, NsdManager.PROTOCOL_DNS_SD, registrationListener)
        } catch (e: Exception) {
            Log.e(TAG, "Exception during registerService: ${e.message}")
        }
    }

    private fun unregisterService() {
        if (!isRegistered || nsdManager == null) return
        val listener = registrationListener ?: return
        try {
            nsdManager.unregisterService(listener)
        } catch (e: Exception) {
            Log.w(TAG, "Exception during unregisterService: ${e.message}")
        } finally {
            isRegistered = false
            registrationListener = null
        }
    }

    private fun discoverServices() {
        if (isDiscovering || nsdManager == null) return

        discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(regType: String) {
                isDiscovering = true
                Log.i(TAG, "Ferry service discovery started for type: $regType")
            }

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                Log.d(TAG, "Service found: ${serviceInfo.serviceName} (${serviceInfo.serviceType})")
                // Check if this is our own service name
                if (serviceInfo.serviceName.contains(deviceId.take(8))) {
                    Log.d(TAG, "Ignoring self advertisement.")
                    return
                }
                enqueueResolve(serviceInfo)
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                Log.i(TAG, "Service lost: ${serviceInfo.serviceName}")
                val devId = serviceNameToDeviceId.remove(serviceInfo.serviceName)
                if (devId != null) {
                    deviceMap.remove(devId)
                    updateDevicesFlow()
                }
            }

            override fun onDiscoveryStopped(serviceType: String) {
                isDiscovering = false
                Log.i(TAG, "Ferry service discovery stopped.")
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                isDiscovering = false
                Log.e(TAG, "Discovery start failed with code: $errorCode")
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "Discovery stop failed with code: $errorCode")
            }
        }

        try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
        } catch (e: Exception) {
            Log.e(TAG, "Exception during discoverServices: ${e.message}")
        }
    }

    private fun stopDiscovery() {
        if (!isDiscovering || nsdManager == null) return
        val listener = discoveryListener ?: return
        try {
            nsdManager.stopServiceDiscovery(listener)
        } catch (e: Exception) {
            Log.w(TAG, "Exception during stopServiceDiscovery: ${e.message}")
        } finally {
            isDiscovering = false
            discoveryListener = null
        }
    }

    private fun enqueueResolve(serviceInfo: NsdServiceInfo) {
        resolveQueue.offer(serviceInfo)
        processNextResolve()
    }

    @Synchronized
    private fun processNextResolve() {
        if (isResolving || nsdManager == null) return
        val nextService = resolveQueue.poll() ?: return

        isResolving = true
        try {
            nsdManager.resolveService(nextService, object : NsdManager.ResolveListener {
                override fun onServiceResolved(resolvedInfo: NsdServiceInfo) {
                    isResolving = false
                    val device = DiscoveredDevice.fromNsdServiceInfo(resolvedInfo)
                    if (device != null && device.deviceId != deviceId) {
                        Log.i(TAG, "Resolved Ferry peer: ${device.deviceName} (${device.osName}) at ${device.host}:${device.port}")
                        deviceMap[device.deviceId] = device
                        serviceNameToDeviceId[resolvedInfo.serviceName] = device.deviceId
                        updateDevicesFlow()
                    }
                    processNextResolve()
                }

                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    isResolving = false
                    Log.w(TAG, "Failed to resolve service ${serviceInfo.serviceName}, code: $errorCode")
                    processNextResolve()
                }
            })
        } catch (e: Exception) {
            isResolving = false
            Log.w(TAG, "Exception during resolveService: ${e.message}")
            processNextResolve()
        }
    }

    private fun updateDevicesFlow() {
        scope.launch {
            _discoveredDevices.value = deviceMap.values.toList()
        }
    }
}
