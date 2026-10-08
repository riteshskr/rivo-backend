import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:stringee_plugin/stringee_plugin.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'firebase_options.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:flutter_polyline_points/flutter_polyline_points.dart';
import 'package:geolocator/geolocator.dart';
import 'package:permission_handler/permission_handler.dart';

// --- CONSTANTS ---
class AppConstants {
  static const String baseUrl = 'https://rivo-api-ezoo.onrender.com';
  static const String notifChannelId = 'ride_channel_v6_clean';
  static const String notifChannelName = 'New Ride Alerts V6';
}

final FlutterLocalNotificationsPlugin _notif = FlutterLocalNotificationsPlugin();

// --- NOTIFICATION SERVICE (CLEAN) ---
class NotificationService {
  static Future<void> init() async {
    const androidInit = AndroidInitializationSettings('@mipmap/ic_launcher');
    await _notif.initialize(const InitializationSettings(android: androidInit));
    final androidPlugin = _notif.resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>();
    const channel = AndroidNotificationChannel(
      AppConstants.notifChannelId,
      AppConstants.notifChannelName,
      importance: Importance.max,
      sound: RawResourceAndroidNotificationSound('alert'),
      playSound: true,
      enableVibration: true,
    );
    await androidPlugin?.createNotificationChannel(channel);
  }

  static Future<void> showAlert({
    required int rideId,
    String? rideVehicleType,
    String? rideCategory,
    String? currency,
    int? seatsBooked,
    int? totalSeats,
  }) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      String myVehicle = prefs.getString('vehicle_type')?? '';
      if (rideVehicleType!= null && rideVehicleType.isNotEmpty && myVehicle.isNotEmpty) {
        if (rideVehicleType.toLowerCase()!= myVehicle.toLowerCase()) return;
      }

      String type = (rideCategory?? '').toLowerCase();
      String curr = currency?? '₹';
      String seatInfo = "";
      if (type.contains('pool') && totalSeats!= null) {
        seatInfo = " ${seatsBooked?? 1}/$totalSeats Seats";
      }

      String title;
      String body;
      if (type.contains('pool')) {
        title = '👥 POOL ${rideVehicleType?? ''}$seatInfo';
        body = 'नया Pool राइड पास में है$seatInfo - $curr';
      } else if (type.contains('parcel') || type.contains('courier')) {
        title = '📦 PARCEL ${rideVehicleType?? ''}';
        body = 'नया Parcel पास में है - $curr';
      } else {
        title = '🚗 RIDE ${rideVehicleType?? ''}';
        body = 'नया राइड पास में है - $curr';
      }

      final androidDetails = AndroidNotificationDetails(
        AppConstants.notifChannelId,
        AppConstants.notifChannelName,
        importance: Importance.max,
        priority: Priority.high,
        sound: const RawResourceAndroidNotificationSound('alert'),
        playSound: true,
        enableVibration: true,
        styleInformation: BigTextStyleInformation(body),
      );
      // FIX: rideId को ही notification id बनाया
      await _notif.show(rideId, title, body, NotificationDetails(android: androidDetails));
    } catch (e) {
      debugPrint("Notif error $e");
    }
  }
}

// --- API SERVICE ---
class ApiService {
  static Future<void> updateDriverLocation({required String driverId, required double lat, required double lng}) async {
    if (driverId.isEmpty) return;
    try {
      final url = Uri.parse('${AppConstants.baseUrl}/drivers/$driverId/location');
      await http.put(url, headers: {'Content-Type': 'application/json'}, body: json.encode({"latitude": lat, "longitude": lng})).timeout(const Duration(seconds: 5));
    } catch (_) {}
  }

  static Future<Map<String, dynamic>?> getRouteFromBackend({required LatLng origin, required LatLng dest}) async {
    try {
      final url = Uri.parse('${AppConstants.baseUrl}/maps/directions?origin=${origin.latitude},${origin.longitude}&dest=${dest.latitude},${dest.longitude}');
      final res = await http.get(url).timeout(const Duration(seconds: 10));
      if (res.statusCode == 200) {
        var body = json.decode(res.body);
        if (body['points']!= null && body['points'].toString().isNotEmpty) {
          return Map<String, dynamic>.from(body);
        }
      }
    } catch (_) {}
    return null;
  }
}

@pragma('vm:entry-point')
Future<void> _firebaseBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
  await NotificationService.init();
  try {
    int rideId = int.tryParse(message.data['ride_id']?.toString()?? "")?? DateTime.now().millisecondsSinceEpoch ~/ 1000;
    await NotificationService.showAlert(
      rideId: rideId,
      rideVehicleType: message.data['vehicle_type']?.toString(),
      rideCategory: message.data['trip_type']?.toString(),
      seatsBooked: int.tryParse(message.data['seats_booked']?.toString()?? "1"),
      totalSeats: int.tryParse(message.data['total_seats']?.toString()?? "3"),
    );
  } catch (_) {}
}

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
  await dotenv.load(fileName: ".env");
  await NotificationService.init();
  FirebaseMessaging.onBackgroundMessage(_firebaseBackgroundHandler);
  runApp(const MyApp());
}

// --- STRINGEE SERVICE (CLEANED) ---
class StringeeService {
  static StringeeClient? _client;
  static StringeeCall? _call;
  static bool _isConnected = false;
  static String? _lastToken;
  static Completer<bool>? _connectionCompleter;
  static StreamSubscription? _clientSub;
  static StreamSubscription? _callSub;
  static Function(String state)? onSignalingStateChanged;
  static Function(StringeeCall)? onIncomingCall;
  static bool get isConnected => _isConnected;

  static Future<bool> connectWithToken(String? token) async {
    if (token == null || token.isEmpty || token == "null") return false;
    if (_isConnected && _client!= null && _lastToken == token) return true;
    if (_lastToken!= null && _lastToken!= token) await disconnect();

    if (_connectionCompleter!= null &&!_connectionCompleter!.isCompleted) {
      _connectionCompleter!.complete(false);
    }
    _connectionCompleter = Completer<bool>();
    _lastToken = token;
    _clientSub?.cancel();
    _client = StringeeClient();
    _clientSub = _client?.eventStreamController.stream.listen((event) {
      switch (event['eventType']) {
        case StringeeClientEvents.didConnect:
          _isConnected = true;
          if (!(_connectionCompleter!.isCompleted)) _connectionCompleter!.complete(true);
          break;
        case StringeeClientEvents.didDisconnect:
        case StringeeClientEvents.didFailWithError:
          _isConnected = false;
          if (!(_connectionCompleter!.isCompleted)) _connectionCompleter!.complete(false);
          break;
        case StringeeClientEvents.incomingCall:
          _call = event['body'];
          _listenToCallEvents();
          if (onIncomingCall!= null) onIncomingCall!(_call!);
          break;
      }
    });
    _client?.connect(token);
    return _connectionCompleter!.future.timeout(const Duration(seconds: 15), onTimeout: () => false);
  }

  static void _listenToCallEvents() {
    _callSub?.cancel();
    _callSub = _call?.eventStreamController.stream.listen((event) {
      if (event['eventType'] == StringeeCallEvents.didChangeSignalingState && onSignalingStateChanged!= null) {
        onSignalingStateChanged!(event['body'].toString());
      }
    });
  }

  static void makeCall(String toUserId) {
    if (!_isConnected || _client == null) {
      onSignalingStateChanged?.call("not_connected");
      return;
    }
    _call = StringeeCall(_client!);
    _listenToCallEvents();
    _call?.makeCall({'from': _client?.userId, 'to': toUserId, 'isVideoCall': false});
  }

  static void answerCall() { _call?.initAnswer(); _call?.answer(); }
  static void rejectCall() { _call?.reject(); _call = null; }
  static void hangup() { _call?.hangup(); _call = null; }

  static Future<void> disconnect() async {
    try {
      _callSub?.cancel();
      _clientSub?.cancel();
      _client?.disconnect();
    } catch (_) {}
    _isConnected = false;
    _lastToken = null;
    _client = null;
    _connectionCompleter = null;
    await Future.delayed(const Duration(milliseconds: 800));
  }
}

class LocationHelper {
  static Future<Position?> getCurrentLocation() async {
    bool serviceEnabled = await Geolocator.isLocationServiceEnabled();
    if (!serviceEnabled) return null;
    LocationPermission permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
      if (permission == LocationPermission.denied) return null;
    }
    if (permission == LocationPermission.deniedForever) return null;
    return await Geolocator.getCurrentPosition(desiredAccuracy: LocationAccuracy.high);
  }
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(debugShowCheckedModeBanner: false, theme: ThemeData(primarySwatch: Colors.green), home: const SplashScreen());
  }
}

class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});
  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen> {
  @override
  void initState() {
    super.initState();
    _checkLogin();
  }

  Future<void> _checkLogin() async {
    final prefs = await SharedPreferences.getInstance();
    bool isLoggedIn = prefs.getBool('isLoggedIn')?? false;
    String? driverId = prefs.getString('driverId');
    String? driverName = prefs.getString('driverName');
    if (isLoggedIn && driverId!= null) {
      try {
        final res = await http.get(Uri.parse('${AppConstants.baseUrl}/drivers/$driverId/active-ride')).timeout(const Duration(seconds: 15));
        if (res.statusCode == 200) {
          final data = json.decode(res.body);
          if (data['active'] == true && data['ride']!= null) {
            if (!mounted) return;
            Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => DriverDashboard(driverId: driverId, driverName: driverName?? 'Driver', initialRide: data['ride'])));
            return;
          }
        }
      } catch (_) {}
      if (!mounted) return;
      Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => DriverDashboard(driverId: driverId, driverName: driverName?? 'Driver')));
    } else {
      if (!mounted) return;
      Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const LoginScreen()));
    }
  }
  @override
  Widget build(BuildContext context) => const Scaffold(body: Center(child: CircularProgressIndicator()));
}

// LoginScreen वही रहेगा, सिर्फ़ UI clean किया है
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _idController = TextEditingController();
  final _passController = TextEditingController();
  bool _loading = false;

  Future<String?> _getFcmToken() async {
    try {
      await FirebaseMessaging.instance.requestPermission(alert: true, sound: true, badge: true);
      return await FirebaseMessaging.instance.getToken();
    } catch (_) { return null; }
  }

  Future<void> _login() async {
    if (_idController.text.trim().isEmpty || _passController.text.trim().isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('ID और Password दोनों डालो')));
      return;
    }
    setState(() => _loading = true);
    try {
      String? fcmToken = await _getFcmToken();
      final res = await http.post(Uri.parse('${AppConstants.baseUrl}/drivers/login'),
          headers: {'Content-Type': 'application/json'},
          body: json.encode({
            "driver_id": _idController.text.trim(),
            "phone": _idController.text.trim(),
            "password": _passController.text.trim(),
            "fcm_token": fcmToken
          })).timeout(const Duration(seconds: 30));

      if (res.statusCode == 200) {
        final data = json.decode(res.body);
        final driver = data.containsKey('driver')? data['driver'] : data;
        String dName = driver['name']?.toString()?? 'Driver';
        String dId = driver['id']?.toString()?? _idController.text.trim();
        double dRange = double.tryParse(driver['range'].toString())?? 20.0;
        final prefs = await SharedPreferences.getInstance();
        await prefs.setBool('isLoggedIn', true);
        await prefs.setString('driverId', dId);
        await prefs.setString('driverName', dName);
        await prefs.setString('vehicle_type', driver['vehicle_type']?.toString()?? 'Car');
        await prefs.setDouble('driver_range', dRange);
        if (!mounted) return;
        Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => DriverDashboard(driverId: dId, driverName: dName)));
      } else {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Login Fail: ${res.body}')));
      }
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
        appBar: AppBar(title: const Text('Driver Login'), backgroundColor: Colors.green),
        body: Padding(
            padding: const EdgeInsets.all(20),
            child: Column(children: [
              TextField(controller: _idController, decoration: const InputDecoration(labelText: 'Driver ID', border: OutlineInputBorder(), prefixIcon: Icon(Icons.person))),
              const SizedBox(height: 12),
              TextField(controller: _passController, obscureText: true, decoration: const InputDecoration(labelText: 'Password', border: OutlineInputBorder(), prefixIcon: Icon(Icons.lock))),
              const SizedBox(height: 20),
              SizedBox(width: double.infinity, child: ElevatedButton(onPressed: _loading? null : _login, style: ElevatedButton.styleFrom(backgroundColor: Colors.green, padding: const EdgeInsets.symmetric(vertical: 14)), child: _loading? const CircularProgressIndicator(color: Colors.white) : const Text('Login', style: TextStyle(color: Colors.white))))
            ])));
  }
}

// DriverDashboard का बाकी logic same है, सिर्फ clean किया गया है
class DriverDashboard extends StatefulWidget {
  final String driverId, driverName;
  final Map<String, dynamic>? initialRide;
  const DriverDashboard({super.key, required this.driverId, required this.driverName, this.initialRide});
  @override
  State<DriverDashboard> createState() => _DriverDashboardState();
}

class _DriverDashboardState extends State<DriverDashboard> {
  List _pending = [];
  Map<String, dynamic>? _activeRide;
  String _callStatus = "Idle";
  WebSocketChannel? _wsChannel;
  bool _loading = true;
  double _myRange = 20.0;
  final _otpController = TextEditingController();
  bool _isOtpVerified = false;
  StreamSubscription<Position>? _locationStream;
  bool _wsShouldReconnect = true;
  Set<int> _alreadyNotifiedIds = {};

  @override
  void initState() {
    super.initState();
    _loadRangeFromPrefs();
    if (widget.initialRide!= null) {
      _activeRide = widget.initialRide;
      if (_activeRide!['status'] == 'started') _isOtpVerified = true;
      _loading = false;
      _connectStringeeFromRide();
    } else {
      _fetchActiveRide();
    }
    _fetchPendingWithRangeCheck();
    _connectWsGlobal();
    _initFcmListeners();
    _startLiveTracking();
    StringeeService.onIncomingCall = (call) {
      if (!mounted) return;
      showDialog(context: context, barrierDismissible: false, builder: (ctx) => AlertDialog(title: const Text("📞 User Call"), content: const Text("User calling you"), actions: [TextButton(onPressed: () { StringeeService.rejectCall(); Navigator.pop(ctx); setState(() => _callStatus = "Rejected"); }, child: const Text("Cut", style: TextStyle(color: Colors.red))), ElevatedButton(onPressed: () { StringeeService.answerCall(); Navigator.pop(ctx); setState(() => _callStatus = "✅ Connected"); }, style: ElevatedButton.styleFrom(backgroundColor: Colors.green), child: const Text("Answer", style: TextStyle(color: Colors.white)))]));
    };
    StringeeService.onSignalingStateChanged = (state) { if (mounted) setState(() => _callStatus = state); };
  }

  Future<void> _connectStringeeFromRide() async {
    String? token = _activeRide?['driver_stringee_token']?.toString();
    if (token!= null && token.isNotEmpty) await StringeeService.connectWithToken(token);
  }

  Future<void> _loadRangeFromPrefs() async {
    final prefs = await SharedPreferences.getInstance();
    double r = prefs.getDouble('driver_range')?? 20.0;
    if (mounted) setState(() => _myRange = r);
  }

  void _startLiveTracking() {
    _locationStream = Geolocator.getPositionStream(locationSettings: const LocationSettings(accuracy: LocationAccuracy.high, distanceFilter: 25)).listen((pos) {
      ApiService.updateDriverLocation(driverId: widget.driverId, lat: pos.latitude, lng: pos.longitude);
    });
  }

  void _initFcmListeners() {
    FirebaseMessaging.onMessage.listen((message) async {
      int rideId = int.tryParse(message.data['ride_id']?.toString()?? "")?? 999;
      await NotificationService.showAlert(rideId: rideId, rideVehicleType: message.data['vehicle_type']?.toString(), rideCategory: message.data['trip_type']?.toString(), seatsBooked: int.tryParse(message.data['seats_booked']?.toString()?? "1"), totalSeats: int.tryParse(message.data['total_seats']?.toString()?? "3"));
      _fetchPendingWithRangeCheck();
    });
  }

  void _connectWsGlobal() async {
    if (!_wsShouldReconnect) return;
    try {
      final prefs = await SharedPreferences.getInstance();
      String vType = prefs.getString('vehicle_type')?? '';
      var pos = await LocationHelper.getCurrentLocation();
      String lat = pos?.latitude.toString()?? '';
      String lng = pos?.longitude.toString()?? '';
      final wsUrl = AppConstants.baseUrl.replaceFirst('https://', 'wss://') + '/ws/drivers?vehicle_type=$vType&lat=$lat&lng=$lng&driver_id=${widget.driverId}';
      _wsChannel = WebSocketChannel.connect(Uri.parse(wsUrl));
      _wsChannel!.stream.listen((msg) async {
        final data = json.decode(msg);
        if (data['type'] == 'new_ride_alert') {
          int rideId = int.tryParse(data['ride']?['id']?.toString()?? data['ride_id']?.toString()?? "0")?? 0;
          await NotificationService.showAlert(rideId: rideId, rideVehicleType: data['ride']?['vehicle_type']?.toString(), rideCategory: data['ride']?['trip_type']?.toString(), currency: data['ride']?['currency']?.toString(), seatsBooked: int.tryParse(data['ride']?['seats_booked']?.toString()?? "1"), totalSeats: int.tryParse(data['ride']?['total_seats']?.toString()?? "3"));
          _fetchPendingWithRangeCheck();
        }
      }, onDone: () { if (_wsShouldReconnect) Future.delayed(const Duration(seconds: 5), _connectWsGlobal); }, onError: (e) { if (_wsShouldReconnect) Future.delayed(const Duration(seconds: 5), _connectWsGlobal); });
    } catch (_) {
      if (_wsShouldReconnect) Future.delayed(const Duration(seconds: 5), _connectWsGlobal);
    }
  }

  Future<void> _fetchActiveRide() async {
    try {
      final res = await http.get(Uri.parse('${AppConstants.baseUrl}/drivers/${widget.driverId}/active-ride')).timeout(const Duration(seconds: 15));
      if (res.statusCode == 200) {
        final data = json.decode(res.body);
        if (data['active'] == true && data['ride']!= null) {
          if (mounted) setState(() { _activeRide = data['ride']; if (_activeRide!['status'] == 'started') _isOtpVerified = true; });
          _connectStringeeFromRide();
        }
      }
    } catch (_) {} finally { if (mounted) setState(() => _loading = false); }
  }

  Future<void> _fetchPendingWithRangeCheck() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      String vType = prefs.getString('vehicle_type')?? '';
      double savedRange = prefs.getDouble('driver_range')?? 20.0;
      if (mounted) setState(() => _myRange = savedRange);
      var pos = await LocationHelper.getCurrentLocation();
      String url = '${AppConstants.baseUrl}/rides/pending/list?driver_id=${widget.driverId}';
      if (vType.isNotEmpty) url += '&vehicle_type=$vType';
      if (pos!= null) url += '&driver_lat=${pos.latitude}&driver_lng=${pos.longitude}';
      final res = await http.get(Uri.parse(url)).timeout(const Duration(seconds: 30));
      if (res.statusCode == 200) {
        var list = json.decode(res.body) as List;
        List filtered = [];
        for (var r in list) {
          double dist = double.tryParse(r['distance_from_driver']?.toString()?? "0")?? 0;
          if (dist <= _myRange) {
            filtered.add(r);
            int rideId = int.tryParse(r['id'].toString())?? 0;
            if (!_alreadyNotifiedIds.contains(rideId)) {
              _alreadyNotifiedIds.add(rideId);
              await NotificationService.showAlert(rideId: rideId, rideVehicleType: r['vehicle_type']?.toString(), rideCategory: r['trip_type']?.toString(), currency: r['currency']?.toString(), seatsBooked: int.tryParse(r['seats_booked']?.toString()?? "1"), totalSeats: int.tryParse(r['total_seats']?.toString()?? r['max_pool_seats']?.toString()?? "3"));
            }
          }
        }
        if (mounted) setState(() { _pending = filtered; _loading = false; });
      }
    } catch (_) { if (mounted) setState(() => _loading = false); }
  }

  Future<void> _acceptRide(Map ride) async {
    try {
      int rideId = int.tryParse(ride['id'].toString())?? 0;
      await _notif.cancel(rideId);
      if (mounted) setState(() { _pending.removeWhere((r) => r['id'] == ride['id']); });
      final res = await http.put(Uri.parse('${AppConstants.baseUrl}/rides/${ride['id']}/accept?driver_id=${widget.driverId}')).timeout(const Duration(seconds: 30));
      if (res.statusCode == 200) {
        final data = json.decode(res.body);
        String? token = data['driver_token']?.toString()?? data['stringee_token']?.toString()?? data['ride']?['driver_stringee_token']?.toString();
        if (token!= null && token.isNotEmpty && token!= "null") {
          await StringeeService.connectWithToken(token);
        }
        if (mounted) setState(() { _activeRide = data['ride']; _isOtpVerified = false; _otpController.clear(); });
      } else { _fetchPendingWithRangeCheck(); }
    } catch (e) { _fetchPendingWithRangeCheck(); }
  }

  Future<void> _verifyOtp() async {
    if (_otpController.text.trim().length!= 6) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('6 अंकों का OTP डालो')));
      return;
    }
    try {
      final res = await http.post(Uri.parse('${AppConstants.baseUrl}/rides/${_activeRide!['id']}/verify-otp'), headers: {'Content-Type': 'application/json'}, body: json.encode({"otp": _otpController.text.trim()})).timeout(const Duration(seconds: 30));
      if (res.statusCode == 200) { if (mounted) setState(() => _isOtpVerified = true); } else { if (mounted) ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('❌ गलत OTP'))); }
    } catch (_) {}
  }

  Future<bool> checkMicPermission() async {
    var status = await Permission.microphone.status;
    if (!status.isGranted) status = await Permission.microphone.request();
    return status.isGranted;
  }

  Future<void> _callUser() async {
    if (_activeRide == null) return;
    bool hasMic = await checkMicPermission();
    if (!hasMic) { if (mounted) ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Mic permission दो, तभी कॉल लगेगी'))); return; }
    String riderId = _activeRide!['stringee_user_id']?.toString()?? _activeRide!['user_id'].toString();
    if (!StringeeService.isConnected) {
      String? token = _activeRide!['driver_stringee_token']?.toString();
      if (token!= null) await StringeeService.connectWithToken(token);
    }
    StringeeService.makeCall(riderId);
  }

  void _hangup() { StringeeService.hangup(); if (mounted) setState(() => _callStatus = "Call Ended"); }

  Future<void> _completeRide() async {
    if (_activeRide == null) return;
    if (!_isOtpVerified) { ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('पहले OTP Verify करो!'))); return; }
    await http.put(Uri.parse('${AppConstants.baseUrl}/rides/${_activeRide!['id']}/complete'));
    await StringeeService.disconnect();
    if (mounted) setState(() { _activeRide = null; _callStatus = "Idle"; _isOtpVerified = false; _otpController.clear(); });
    _fetchPendingWithRangeCheck();
  }

  @override
  void dispose() {
    _wsShouldReconnect = false;
    _locationStream?.cancel();
    _wsChannel?.sink.close();
    _otpController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
        appBar: AppBar(title: Text('${widget.driverName} - ${_myRange.toInt()}km'), backgroundColor: Colors.green, actions: [IconButton(icon: const Icon(Icons.logout), onPressed: () async { final p = await SharedPreferences.getInstance(); await p.clear(); await StringeeService.disconnect(); if (!mounted) return; Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const LoginScreen())); })]),
        body: _activeRide == null? Column(children: [Container(width: double.infinity, padding: const EdgeInsets.all(10), color: Colors.green.shade50, child: Text('Online - ${_pending.length} Rides | $_callStatus', style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13))), Expanded(child: _buildPendingList())]) : _ActiveRideWithMap(ride: _activeRide!, driverId: widget.driverId, isOtpVerified: _isOtpVerified, otpController: _otpController, onVerifyOtp: _verifyOtp, onComplete: _completeRide, onCall: _callUser, onHangup: _hangup, callStatus: _callStatus));
  }

  Widget _buildPendingList() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_pending.isEmpty) return Center(child: Text('कोई राइड नहीं है - ${_myRange.toInt()} km में खोज रहे हैं', style: TextStyle(color: Colors.grey)));
    return ListView.builder(padding: const EdgeInsets.all(8), itemCount: _pending.length, itemBuilder: (ctx, i) {
      final r = _pending[i];
      double dist = double.tryParse(r['distance_from_driver']?.toString()?? "0")?? 0;
      return Card(margin: const EdgeInsets.only(bottom: 12), child: Padding(padding: const EdgeInsets.all(12), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text("${r['trip_type']} - ₹${r['fare']} | ${dist.toStringAsFixed(1)}km", style: TextStyle(fontWeight: FontWeight.bold)),
        SizedBox(height: 8),
        Text("📍 ${r['pickup_address']}"),
        Text("🏁 ${r['drop_address']}"),
        SizedBox(height: 10),
        Row(children: [
          Expanded(child: OutlinedButton.icon(onPressed: () { Navigator.push(context, MaterialPageRoute(builder: (_) => RideMapView(ride: r))); }, icon: Icon(Icons.map, size: 16), label: Text("Map"))),
          SizedBox(width: 10),
          Expanded(child: ElevatedButton(onPressed: () => _acceptRide(r), style: ElevatedButton.styleFrom(backgroundColor: Colors.green), child: Text('Accept', style: TextStyle(color: Colors.white)))),
        ])
      ])));
    });
  }
}

// RideMapView और _ActiveRideWithMap का logic वही रखा है, बस Safe Parsing add किया है
//... (बाकी के 2 Map Widget आपके origin file वाले ही use कर सकते हो, वो पहले से ठीक हैं)

class RideMapView extends StatefulWidget {
  final Map ride; const RideMapView({super.key, required this.ride});
  @override State<RideMapView> createState() => _RideMapViewState();
}
class _RideMapViewState extends State<RideMapView> {
  GoogleMapController? mapController; LatLng? driverLatLng, pickupLatLng, dropLatLng; Set<Marker> markers = {}; Set<Polyline> polylines = {}; String distanceText = "Loading..."; bool loading = true;
  @override void initState() { super.initState(); _initMap(); }
  Future<void> _initMap() async {
    double pLat = double.tryParse(widget.ride['pickup_lat']?.toString()?? "0")?? 0;
    double pLng = double.tryParse(widget.ride['pickup_lng']?.toString()?? widget.ride['pickup_lng']?.toString()?? "0")?? 0;
    double dLat = double.tryParse(widget.ride['drop_lat']?.toString()?? "0")?? 0;
    double dLng = double.tryParse(widget.ride['drop_lng']?.toString()?? "0")?? 0;
    pickupLatLng = LatLng(pLat, pLng); dropLatLng = LatLng(dLat, dLng);
    var pos = await LocationHelper.getCurrentLocation();
    if (pos!= null) driverLatLng = LatLng(pos.latitude, pos.longitude);
    setState(() {
      markers.add(Marker(markerId: MarkerId('pickup'), position: pickupLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueRed)));
      markers.add(Marker(markerId: MarkerId('drop'), position: dropLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueGreen)));
      if (driverLatLng!= null) markers.add(Marker(markerId: MarkerId('driver'), position: driverLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueBlue)));
      loading = false;
    });
    _getRoadSecure();
  }
  Future<void> _getRoadSecure() async {
    if (pickupLatLng == null || dropLatLng == null) return;
    try {
      var route = await ApiService.getRouteFromBackend(origin: pickupLatLng!, dest: dropLatLng!);
      if (route!= null && route['points']!= null) {
        var decoded = PolylinePoints().decodePolyline(route['points']);
        var pts = decoded.map((e) => LatLng(e.latitude, e.longitude)).toList();
        if (pts.isNotEmpty) {
          polylines.add(Polyline(polylineId: PolylineId('route'), points: pts, color: Colors.black, width: 6));
          distanceText = "${route['distance_text']?? ''} ${route['duration_text']?? ''}";
        }
      }
      if (mounted) setState(() {});
    } catch (_) {}
  }
  @override Widget build(BuildContext context) {
    return Scaffold(appBar: AppBar(title: Text(distanceText, style: TextStyle(fontSize: 12)), backgroundColor: Colors.green), body: loading? Center(child: CircularProgressIndicator()) : GoogleMap(initialCameraPosition: CameraPosition(target: pickupLatLng!, zoom: 13), markers: markers, polylines: polylines, myLocationEnabled: true, onMapCreated: (c) => mapController = c));
  }
}

class _ActiveRideWithMap extends StatefulWidget {
  final Map ride; final String driverId; final bool isOtpVerified; final TextEditingController otpController; final VoidCallback onVerifyOtp, onComplete, onCall, onHangup; final String callStatus;
  const _ActiveRideWithMap({required this.ride, required this.driverId, required this.isOtpVerified, required this.otpController, required this.onVerifyOtp, required this.onComplete, required this.onCall, required this.onHangup, required this.callStatus});
  @override State<_ActiveRideWithMap> createState() => _ActiveRideWithMapState();
}
class _ActiveRideWithMapState extends State<_ActiveRideWithMap> {
  GoogleMapController? mapController; LatLng? driverLatLng, pickupLatLng, dropLatLng; Set<Marker> markers = {}; Set<Polyline> polylines = {};
  @override void initState() { super.initState(); _initMap(); }
  Future<void> _initMap() async {
    pickupLatLng = LatLng(double.tryParse(widget.ride['pickup_lat'].toString())?? 0, double.tryParse(widget.ride['pickup_lng'].toString())?? 0);
    dropLatLng = LatLng(double.tryParse(widget.ride['drop_lat'].toString())?? 0, double.tryParse(widget.ride['drop_lng'].toString())?? 0);
    var pos = await LocationHelper.getCurrentLocation(); if (pos!= null) driverLatLng = LatLng(pos.latitude, pos.longitude);
    setState(() {
      markers = {Marker(markerId: MarkerId('pickup'), position: pickupLatLng!), Marker(markerId: MarkerId('drop'), position: dropLatLng!), if (driverLatLng!= null) Marker(markerId: MarkerId('driver'), position: driverLatLng!)};
    });
  }
  @override Widget build(BuildContext context) {
    return Column(children: [
      Expanded(flex: 2, child: GoogleMap(initialCameraPosition: CameraPosition(target: pickupLatLng!, zoom: 14), markers: markers, polylines: polylines, onMapCreated: (c) => mapController = c)),
      Expanded(flex: 2, child: Padding(padding: EdgeInsets.all(12), child: Column(children: [
        Text("#${widget.ride['id']} - ₹${widget.ride['fare']}", style: TextStyle(fontWeight: FontWeight.bold)),
        Text("${widget.ride['pickup_address']} -> ${widget.ride['drop_address']}"),
        SizedBox(height: 10),
        Row(children: [Expanded(child: ElevatedButton(onPressed: widget.onCall, child: Text("Call"))), SizedBox(width: 8), Expanded(child: ElevatedButton(onPressed: widget.onHangup, style: ElevatedButton.styleFrom(backgroundColor: Colors.red), child: Text("Cut")))]),
        Text(widget.callStatus),
        if (!widget.isOtpVerified)...[TextField(controller: widget.otpController, keyboardType: TextInputType.number, maxLength: 6, decoration: InputDecoration(hintText: 'OTP')), ElevatedButton(onPressed: widget.onVerifyOtp, child: Text("Verify OTP"))] else ElevatedButton(onPressed: widget.onComplete, child: Text("Complete Ride"))
      ])))
    ]);
  }
}

