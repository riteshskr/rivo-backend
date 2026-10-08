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

class AppConstants {
  static const String baseUrl = 'https://rivo-api-ezoo.onrender.com';
  static const String notifChannelId = 'ride_channel_v5';
  static const String notifChannelName = 'New Ride Alerts V5';
}

final FlutterLocalNotificationsPlugin _notif = FlutterLocalNotificationsPlugin();

class NotificationService {
  static Future<void> init() async {
    const androidInit = AndroidInitializationSettings('@mipmap/ic_launcher');
    await _notif.initialize(const InitializationSettings(android: androidInit));
    final androidPlugin = _notif.resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>();
    const channel = AndroidNotificationChannel(AppConstants.notifChannelId, AppConstants.notifChannelName, importance: Importance.max, sound: RawResourceAndroidNotificationSound('alert'), playSound: true, enableVibration: true);
    await androidPlugin?.createNotificationChannel(channel);
  }

  static Future<void> showAlert({String? rideVehicleType, String? rideCategory, String? currency, int? seatsBooked, int? totalSeats}) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      String myVehicle = prefs.getString('vehicle_type')?? '';
      if (rideVehicleType!= null && rideVehicleType.isNotEmpty && myVehicle.isNotEmpty) {
        if (rideVehicleType.toLowerCase()!= myVehicle.toLowerCase()) return;
      }
      String type = (rideCategory?? '').toLowerCase();
      String title;
      String body;
      String curr = currency?? '₹';
      String seatInfo = "";
      if(type.contains('pool') && totalSeats!= null){
        seatInfo = " ${seatsBooked??1}/${totalSeats} Seats";
      }
      if (type.contains('pool')) {
        title = '👥 POOL ${rideVehicleType?? ''}$seatInfo - SHARED';
        body = 'New Pool ride nearby$seatInfo - $curr - ${rideVehicleType?? ''}';
      } else if (type.contains('parcel') || type.contains('courier')) {
        title = '📦 PARCEL ${rideVehicleType?? ''}!';
        body = 'New Parcel nearby - $curr';
      } else {
        title = '🚗 RIDE ${rideVehicleType?? ''}!';
        body = 'New ride nearby - $curr';
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
      await _notif.show(
        DateTime.now().millisecond,
        title,
        body,
        NotificationDetails(android: androidDetails),
      );
    } catch (e) {
      print("Notif error $e");
    }
  }
}

class ApiService {
  static Future<void> updateDriverLocation({required String driverId, required double lat, required double lng}) async {
    if (driverId.isEmpty) return;
    try {
      final url = Uri.parse('${AppConstants.baseUrl}/drivers/$driverId/location');
      await http.put(url, headers: {'Content-Type': 'application/json'}, body: json.encode({"latitude": lat, "longitude": lng})).timeout(const Duration(seconds: 5));
    } catch (e) {}
  }

  static Future<Map<String, dynamic>?> getRouteFromBackend({required LatLng origin, required LatLng dest}) async {
    try {
      final url = Uri.parse('${AppConstants.baseUrl}/maps/directions?origin=${origin.latitude},${origin.longitude}&dest=${dest.latitude},${dest.longitude}');
      final res = await http.get(url).timeout(const Duration(seconds: 10));
      if (res.statusCode == 200) {
        var body = json.decode(res.body);
        if (body['points']!= null && body['points'].toString().isNotEmpty) {
          return body;
        }
      }
    } catch (e) {}
    try {
      String googleKey = dotenv.env['GOOGLE_MAPS_API_KEY']?? "";
      if (googleKey.isEmpty) return null;
      final gUrl = Uri.parse('https://maps.googleapis.com/maps/api/directions/json?origin=${origin.latitude},${origin.longitude}&destination=${dest.latitude},${dest.longitude}&key=$googleKey');
      final res = await http.get(gUrl).timeout(const Duration(seconds: 10));
      if (res.statusCode == 200) {
        var data = json.decode(res.body);
        if (data['status'] == 'OK' && data['routes'].isNotEmpty) {
          var route = data['routes'][0];
          var leg = route['legs'][0];
          return {
            "points": route['overview_polyline']['points'],
            "distance_text": leg['distance']['text'],
            "duration_text": leg['duration']['text'],
          };
        }
      }
    } catch (e) {}
    return null;
  }
}

@pragma('vm:entry-point')
Future<void> _firebaseBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
  try {
    String? rideVehicle = message.data['vehicle_type']?.toString();
    String? category = message.data['trip_type']?.toString();
    int? booked = int.tryParse(message.data['seats_booked']?.toString()?? "1");
    int? total = int.tryParse(message.data['total_seats']?.toString()?? "3");
    final prefs = await SharedPreferences.getInstance();
    String myVehicle = prefs.getString('vehicle_type')?? '';
    if (rideVehicle!= null && rideVehicle.isNotEmpty && myVehicle.isNotEmpty) {
      if (rideVehicle.toLowerCase()!= myVehicle.toLowerCase()) return;
    }
    await NotificationService.init();
    await NotificationService.showAlert(rideVehicleType: rideVehicle, rideCategory: category, seatsBooked: booked, totalSeats: total);
  } catch (e) {}
}

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
  await dotenv.load(fileName: ".env");
  await NotificationService.init();
  FirebaseMessaging.onBackgroundMessage(_firebaseBackgroundHandler);
  runApp(const MyApp());
}

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
      var map = event;
      switch (map['eventType']) {
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
          _call = map['body'];
          _listenToCallEvents();
          if (onIncomingCall!= null) onIncomingCall!(_call!);
          break;
      }
    });
    _client?.connect(token);
    return _connectionCompleter!.future.timeout(const Duration(seconds: 15), onTimeout: () {
      _isConnected = false;
      return false;
    });
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
    _callSub?.cancel();
    _clientSub?.cancel();
    _client?.disconnect();
    _isConnected = false;
    _lastToken = null;
    _connectionCompleter = null;
    await Future.delayed(const Duration(milliseconds: 400));
  }
}

class LocationHelper {
  static Future<Position?> getCurrentLocation() async {
    bool serviceEnabled = await Geolocator.isLocationServiceEnabled(); if (!serviceEnabled) return null;
    LocationPermission permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) { permission = await Geolocator.requestPermission(); if (permission == LocationPermission.denied) return null; }
    if (permission == LocationPermission.deniedForever) return null;
    return await Geolocator.getCurrentPosition(desiredAccuracy: LocationAccuracy.high);
  }
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});
  @override Widget build(BuildContext context) { return MaterialApp(debugShowCheckedModeBanner: false, theme: ThemeData(primarySwatch: Colors.green), home: const SplashScreen()); }
}

class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});
  @override State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen> {
  @override void initState() { super.initState(); _checkLogin(); }
  Future<void> _checkLogin() async {
    final prefs = await SharedPreferences.getInstance();
    bool isLoggedIn = prefs.getBool('isLoggedIn')?? false;
    String? driverId = prefs.getString('driverId'); String? driverName = prefs.getString('driverName');
    if (isLoggedIn && driverId!= null) {
      try {
        final res = await http.get(Uri.parse('${AppConstants.baseUrl}/drivers/$driverId/active-ride')).timeout(const Duration(seconds: 15));
        if (res.statusCode == 200) {
          final data = json.decode(res.body);
          if (data['active'] == true && data['ride']!= null) {
            if (!mounted) return;
            Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => DriverDashboard(driverId: driverId, driverName: driverName?? 'Driver', initialRide: data['ride']))); return;
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
  @override Widget build(BuildContext context) => const Scaffold(body: Center(child: CircularProgressIndicator()));
}

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _idController = TextEditingController(text: "");
  final _passController = TextEditingController(text: "");
  bool _loading = false;
  Future<String?> _getFcmToken() async {
    try {
      await FirebaseMessaging.instance.requestPermission(alert: true, sound: true, badge: true);
      return await FirebaseMessaging.instance.getToken();
    } catch (e) { return null; }
  }
  Future<void> _login() async {
    if (_idController.text.trim().isEmpty || _passController.text.trim().isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('ID और Password दोनों डालो')));
      return;
    }
    setState(() => _loading = true);
    try {
      String? fcmToken = await _getFcmToken();
      final res = await http.post(Uri.parse('${AppConstants.baseUrl}/drivers/login'), headers: {'Content-Type': 'application/json'}, body: json.encode({"driver_id": _idController.text.trim(), "phone": _idController.text.trim(), "mobile": _idController.text.trim(), "password": _passController.text.trim(), "fcm_token": fcmToken})).timeout(const Duration(seconds: 90));
      if (res.statusCode == 200) {
        final data = json.decode(res.body);
        final driver = data.containsKey('driver')? data['driver'] : data;
        if (driver == null || driver['id'] == null) throw Exception("Invalid driver data");
        String dName = driver['name']?.toString()?? driver['id']?.toString()?? 'Driver';
        String dId = driver['id']?.toString()?? _idController.text.trim();
        double dRange = double.tryParse(driver['range'].toString())?? 20.0;
        final prefs = await SharedPreferences.getInstance();
        await prefs.setBool('isLoggedIn', true);
        await prefs.setString('driverId', dId);
        await prefs.setString('driverName', dName);
        await prefs.setString('vehicle_type', driver['vehicle_type']?.toString()?? 'Car');
        await prefs.setDouble('driver_range', dRange);
        if (fcmToken!= null) await prefs.setString('fcm_token', fcmToken);
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
  @override Widget build(BuildContext context) {
    return Scaffold(appBar: AppBar(title: const Text('Driver Login'), backgroundColor: Colors.green), body: Padding(padding: const EdgeInsets.all(20), child: Column(children: [TextField(controller: _idController, decoration: const InputDecoration(labelText: 'Driver ID', hintText: 'Enter ID e.g. car1', border: OutlineInputBorder(), prefixIcon: Icon(Icons.person))), const SizedBox(height: 12), TextField(controller: _passController, obscureText: true, decoration: const InputDecoration(labelText: 'Password', hintText: 'Enter password', border: OutlineInputBorder(), prefixIcon: Icon(Icons.lock))), const SizedBox(height: 20), SizedBox(width: double.infinity, child: ElevatedButton(onPressed: _loading? null : _login, style: ElevatedButton.styleFrom(backgroundColor: Colors.green, padding: const EdgeInsets.symmetric(vertical: 14)), child: _loading? const CircularProgressIndicator(color: Colors.white) : const Text('Login', style: TextStyle(color: Colors.white, fontSize: 16))))])));
  }
}

class DriverDashboard extends StatefulWidget {
  final String driverId, driverName; final Map<String, dynamic>? initialRide;
  const DriverDashboard({super.key, required this.driverId, required this.driverName, this.initialRide});
  @override State<DriverDashboard> createState() => _DriverDashboardState();
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
  bool _isVerifying = false;
  StreamSubscription<Position>? _locationStream;
  Set<int> _alreadyNotifiedIds = {};

  @override void initState() {
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
      _notif.cancel(1);
      showDialog(context: context, barrierDismissible: false, builder: (ctx) => AlertDialog(shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)), title: const Text("📞 User Call"), content: const Text("User calling you"), actions: [TextButton(onPressed: () { StringeeService.rejectCall(); Navigator.pop(ctx); setState(() => _callStatus = "Rejected"); }, child: const Text("Cut", style: TextStyle(color: Colors.red, fontSize: 18))), ElevatedButton(onPressed: () { StringeeService.answerCall(); Navigator.pop(ctx); setState(() => _callStatus = "✅ Connected"); }, style: ElevatedButton.styleFrom(backgroundColor: Colors.green), child: const Text("Answer", style: TextStyle(color: Colors.white)))]));
    };
    StringeeService.onSignalingStateChanged = (state) { if (mounted) setState(() => _callStatus = state); };
  }

  Future<void> _connectStringeeFromRide() async {
    String? token = _activeRide?['driver_stringee_token']?.toString();
    if (token!= null && token.isNotEmpty) await StringeeService.connectWithToken(token);
  }

  Future<void> _loadRangeFromPrefs() async { final prefs = await SharedPreferences.getInstance(); double r = prefs.getDouble('driver_range')?? 20.0; if (mounted) setState(() => _myRange = r); }

  void _startLiveTracking() {
    _locationStream = Geolocator.getPositionStream(locationSettings: const LocationSettings(accuracy: LocationAccuracy.high, distanceFilter: 20)).listen((pos) {
      ApiService.updateDriverLocation(driverId: widget.driverId, lat: pos.latitude, lng: pos.longitude);
      _fetchPendingWithRangeCheck();
    });
  }

  void _initFcmListeners() {
    FirebaseMessaging.onMessage.listen((message) async {
      String? vType = message.data['vehicle_type']?.toString();
      String? category = message.data['trip_type']?.toString()?? message.data['type']?.toString()?? message.data['category']?.toString();
      int? booked = int.tryParse(message.data['seats_booked']?.toString()?? "1");
      int? total = int.tryParse(message.data['total_seats']?.toString()?? "3");
      await NotificationService.showAlert(rideVehicleType: vType, rideCategory: category, seatsBooked: booked, totalSeats: total);
      _fetchPendingWithRangeCheck();
    });
    FirebaseMessaging.onMessageOpenedApp.listen((message) { _fetchPendingWithRangeCheck(); });
  }

  void _connectWsGlobal() async {
    try {
      final prefs = await SharedPreferences.getInstance(); String vType = prefs.getString('vehicle_type')?? ''; var pos = await LocationHelper.getCurrentLocation(); String lat = pos?.latitude.toString()?? ''; String lng = pos?.longitude.toString()?? ''; final wsUrl = AppConstants.baseUrl.replaceFirst('https://', 'wss://') + '/ws/drivers?vehicle_type=$vType&lat=$lat&lng=$lng&driver_id=${widget.driverId}'; _wsChannel = WebSocketChannel.connect(Uri.parse(wsUrl)); _wsChannel!.stream.listen((msg) async { final data = json.decode(msg); if (data['type'] == 'new_ride_alert') { String? vType = data['ride']?['vehicle_type']?.toString()?? data['vehicle_type']?.toString(); String? category = data['ride']?['trip_type']?.toString()?? data['type']?.toString()?? data['category']?.toString(); String? currency = data['ride']?['currency']?.toString()?? '₹'; int? booked = int.tryParse(data['ride']?['seats_booked']?.toString()?? "1"); int? total = int.tryParse(data['ride']?['total_seats']?.toString()?? data['ride']?['max_pool_seats']?.toString()?? "3"); await NotificationService.showAlert(rideVehicleType: vType, rideCategory: category, currency: currency, seatsBooked: booked, totalSeats: total); _fetchPendingWithRangeCheck(); } else if (data['type'] == 'ride_taken') { int takenId = int.tryParse(data['ride_id'].toString())?? -1; if (mounted) { setState(() { _pending.removeWhere((r) => r['id'] == takenId); }); } try { await _notif.cancel(takenId); } catch (_) {} } }, onDone: () { Future.delayed(const Duration(seconds: 5), _connectWsGlobal); }, onError: (e) { Future.delayed(const Duration(seconds: 5), _connectWsGlobal); });
    } catch (e) {}
  }

  Future<void> _fetchActiveRide() async { try { final res = await http.get(Uri.parse('${AppConstants.baseUrl}/drivers/${widget.driverId}/active-ride')).timeout(const Duration(seconds: 15)); if (res.statusCode == 200) { final data = json.decode(res.body); if (data['active'] == true && data['ride']!= null) { if (mounted) setState(() { _activeRide = data['ride']; if (_activeRide!['status'] == 'started') _isOtpVerified = true; }); _connectStringeeFromRide(); } } } catch (e) {} finally { if (mounted) setState(() => _loading = false); } }

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
              int booked = int.tryParse(r['seats_booked']?.toString()?? "1")?? 1;
              int total = int.tryParse(r['total_seats']?.toString()?? r['max_pool_seats']?.toString()?? "3")?? 3;
              await NotificationService.showAlert(rideVehicleType: r['vehicle_type']?.toString(), rideCategory: r['trip_type']?.toString(), currency: r['currency']?.toString(), seatsBooked: booked, totalSeats: total);
            }
          }
        }
        if (mounted) setState(() { _pending = filtered; _loading = false; });
      }
    } catch (e) {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _acceptRide(Map ride) async {
    try {
      await _notif.cancel(ride['id']);
      if (mounted) setState(() { _pending.removeWhere((r) => r['id'] == ride['id']); });
      final res = await http.put(Uri.parse('${AppConstants.baseUrl}/rides/${ride['id']}/accept?driver_id=${widget.driverId}')).timeout(const Duration(seconds: 30));
      if (res.statusCode == 200) {
        final data = json.decode(res.body);
        String? token = data['driver_token']?.toString()?? data['stringee_token']?.toString()?? data['ride']?['driver_stringee_token']?.toString();
        if (token!= null && token.isNotEmpty && token!= "null") {
          await StringeeService.connectWithToken(token);
          final prefs = await SharedPreferences.getInstance();
          await prefs.setString('stringee_token', token);
        }
        if (mounted) setState(() { _activeRide = data['ride']; _isOtpVerified = false; _otpController.clear(); });
      } else { _fetchPendingWithRangeCheck(); if (mounted) ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Already Taken'))); }
    } catch (e) { _fetchPendingWithRangeCheck(); if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e'))); }
  }

  Future<void> _verifyOtp() async { if (_otpController.text.trim().length!= 6) { ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Enter 6 digit OTP'))); return; } setState(() => _isVerifying = true); try { final res = await http.post(Uri.parse('${AppConstants.baseUrl}/rides/${_activeRide!['id']}/verify-otp'), headers: {'Content-Type': 'application/json'}, body: json.encode({"otp": _otpController.text.trim()})).timeout(const Duration(seconds: 30)); if (res.statusCode == 200) { if (mounted) setState(() => _isOtpVerified = true); } else { if (mounted) ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('❌ Wrong OTP'))); } } catch (e) {} finally { if (mounted) setState(() => _isVerifying = false); } }
  Future<bool> checkMicPermission() async { var status = await Permission.microphone.status; if (!status.isGranted) { status = await Permission.microphone.request(); } return status.isGranted; }
  Future<void> _callUser() async { if (_activeRide == null) return; bool hasMic = await checkMicPermission(); if (!hasMic) { if (mounted) ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Mic permission दो, तभी कॉल लगेगी'))); return; } String riderId = _activeRide!['stringee_user_id']?.toString()?? ""; if (riderId.isEmpty) { riderId = _activeRide!['user_id'].toString(); } if (!StringeeService.isConnected) { final prefs = await SharedPreferences.getInstance(); String? token = prefs.getString('stringee_token')?? _activeRide!['driver_stringee_token']?.toString(); if (token!= null) await StringeeService.connectWithToken(token); } StringeeService.makeCall(riderId); if (mounted) setState(() => _callStatus = "📞 Calling $riderId..."); }
  void _hangup() { StringeeService.hangup(); if (mounted) setState(() => _callStatus = "Call Ended"); }
  Future<void> _completeRide() async { if (_activeRide == null) return; if (!_isOtpVerified) { ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Verify OTP first!'))); return; } await http.put(Uri.parse('${AppConstants.baseUrl}/rides/${_activeRide!['id']}/complete')); await StringeeService.disconnect(); if (mounted) setState(() { _activeRide = null; _callStatus = "Idle"; _isOtpVerified = false; _otpController.clear(); }); _fetchPendingWithRangeCheck(); }
  @override void dispose() { _locationStream?.cancel(); _wsChannel?.sink.close(); _otpController.dispose(); super.dispose(); }

  Widget _buildPendingList() {
  if (_loading) return const Center(child: CircularProgressIndicator());
  if (_pending.isEmpty) { return Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [const Icon(Icons.search_off, size: 60, color: Colors.grey), const SizedBox(height: 10), const Text('No Rides Nearby', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)), const SizedBox(height: 5), Text('Searching in ${_myRange.toInt()} km range', style: const TextStyle(color: Colors.grey, fontSize: 13))])); }
  return ListView.builder(padding: const EdgeInsets.all(8), itemCount: _pending.length, itemBuilder: (ctx, i) {
    final r = _pending[i];
    double dist = double.tryParse(r['distance_from_driver']?.toString()?? "0")?? 0;
    String tripType = (r['trip_type']?.toString()?? 'ride').toLowerCase();
    bool isParcel = tripType.contains('parcel');
    bool isPool = tripType.contains('pool');
    String curr = r['currency']?.toString()?? '₹';
    Color badgeColor = isPool? Colors.blue : isParcel? Colors.orange : Colors.green;
    String label = isPool? "👥 POOL" : isParcel? "📦 PARCEL" : "🚗 RIDE";
    String pickupAddr = r['pickup_address']?.toString()?? 'Pickup';
    String dropAddr = r['drop_address']?.toString()?? 'Drop';
    int seatsBooked = int.tryParse(r['seats_booked']?.toString()?? "1")?? 1;
    int totalSeats = int.tryParse(r['total_seats']?.toString()?? r['max_pool_seats']?.toString()?? "3")?? 3;
    if(!isPool) totalSeats = 1;
    return Card(margin: const EdgeInsets.only(bottom: 12), elevation: 3, shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)), child: Padding(padding: const EdgeInsets.all(12), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(children: [
        Container(padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3), decoration: BoxDecoration(color: badgeColor, borderRadius: BorderRadius.circular(5)), child: Text(label, style: const TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.bold))),
        const SizedBox(width: 6),
        Container(padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3), decoration: BoxDecoration(color: Colors.grey.shade200, borderRadius: BorderRadius.circular(5)), child: Text("$curr${r['fare']} | ${dist.toStringAsFixed(1)}km", style: const TextStyle(fontSize: 11, fontWeight: FontWeight.bold))),
        if(isPool)...[
          const SizedBox(width: 6),
          Container(padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3), decoration: BoxDecoration(color: Colors.blue.shade50, border: Border.all(color: Colors.blue), borderRadius: BorderRadius.circular(5)), child: Text(r['pool_group_id']?? '', style: const TextStyle(fontSize: 10, color: Colors.blue, fontWeight: FontWeight.bold))),
        ]
      ]),
      if(isPool)...[
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.all(8),
          decoration: BoxDecoration(color: Colors.blue.shade50, borderRadius: BorderRadius.circular(8), border: Border.all(color: Colors.blue.shade200)),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4), decoration: BoxDecoration(color: Colors.blue, borderRadius: BorderRadius.circular(5)), child: Row(children: [Icon(Icons.event_seat, size: 14, color: Colors.white), SizedBox(width: 3), Text("Total: $totalSeats", style: TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.bold))])),
              SizedBox(width: 6),
              Container(padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4), decoration: BoxDecoration(color: Colors.green, borderRadius: BorderRadius.circular(5)), child: Text("Booked: $seatsBooked", style: TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.bold))),
              SizedBox(width: 6),
              Container(padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4), decoration: BoxDecoration(color: Colors.orange, borderRadius: BorderRadius.circular(5)), child: Text("Left: ${totalSeats - seatsBooked}", style: TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.bold))),
            ],
          ),
        ),
      ],
      const SizedBox(height: 10),
      Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Icon(Icons.my_location, color: Colors.green, size: 18),
        const SizedBox(width: 6),
        Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text("PICKUP:", style: TextStyle(fontSize: 9, fontWeight: FontWeight.bold, color: Colors.grey)),
          Text(pickupAddr, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
        ])),
      ]),
      const SizedBox(height: 8),
      Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Icon(Icons.location_on, color: Colors.red, size: 18),
        const SizedBox(width: 6),
        Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text("DROP:", style: TextStyle(fontSize: 9, fontWeight: FontWeight.bold, color: Colors.grey)),
          Text(dropAddr, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
        ])),
      ]),
      const SizedBox(height: 12),
      Row(children: [
        Expanded(child: OutlinedButton.icon(onPressed: () { Navigator.push(context, MaterialPageRoute(builder: (_) => RideMapView(ride: r))); }, icon: const Icon(Icons.map, size: 16), label: const Text("Map देखें", style: TextStyle(fontSize: 12)), style: OutlinedButton.styleFrom(padding: const EdgeInsets.symmetric(vertical: 8)))),
        const SizedBox(width: 10),
        Expanded(child: ElevatedButton(onPressed: () => _acceptRide(r), style: ElevatedButton.styleFrom(backgroundColor: badgeColor, padding: const EdgeInsets.symmetric(vertical: 8)), child: const Text('Accept', style: TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.bold)))),
      ])
    ]))));
  });
  }

  @override Widget build(BuildContext context) {
    return Scaffold(appBar: AppBar(title: Text('${widget.driverName} - ${_myRange.toInt()}km'), backgroundColor: Colors.green, actions: [IconButton(icon: const Icon(Icons.logout), onPressed: () async { final p = await SharedPreferences.getInstance(); await p.clear(); await StringeeService.disconnect(); if (!mounted) return; Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const LoginScreen())); })]), body: _activeRide == null? Column(children: [Container(width: double.infinity, padding: const EdgeInsets.all(10), color: Colors.green.shade50, child: Row(children: [const Icon(Icons.circle, color: Colors.green, size: 12), const SizedBox(width: 6), Text('Online - ${_pending.length} Rides in ${_myRange.toInt()}km | $_callStatus', style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)), const Spacer(), IconButton(icon: const Icon(Icons.refresh, size: 20), onPressed: _fetchPendingWithRangeCheck)])), Expanded(child: Container(margin: const EdgeInsets.all(10), decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(12), border: Border.all(color: Colors.grey.shade300)), child: _buildPendingList()))]) : _ActiveRideWithMap(ride: _activeRide!, driverId: widget.driverId, isOtpVerified: _isOtpVerified, otpController: _otpController, onVerifyOtp: _verifyOtp, onComplete: _completeRide, onCall: _callUser, onHangup: _hangup, callStatus: _callStatus));
  }
}

class RideMapView extends StatefulWidget {
  final Map ride; const RideMapView({super.key, required this.ride});
  @override State<RideMapView> createState() => _RideMapViewState();
}

class _RideMapViewState extends State<RideMapView> {
  GoogleMapController? mapController; LatLng? driverLatLng, pickupLatLng, dropLatLng; Set<Marker> markers = {}; Set<Polyline> polylines = {}; String distanceText = "Route Loading..."; bool loading = true;

  @override void initState() { super.initState(); _initMap(); }

  Future<void> _initMap() async {
    double pLat = double.tryParse(widget.ride['pickup_lat']?.toString()?? widget.ride['pickup_latitude']?.toString()?? "0")?? 0;
    double pLng = double.tryParse(widget.ride['pickup_lng']?.toString()?? widget.ride['pickup_longitude']?.toString()?? widget.ride['pickup_lng']?.toString()?? "0")?? 0;
    double dLat = double.tryParse(widget.ride['drop_lat']?.toString()?? widget.ride['drop_latitude']?.toString()?? "0")?? 0;
    double dLng = double.tryParse(widget.ride['drop_lng']?.toString()?? widget.ride['drop_longitude']?.toString()?? "0")?? 0;

    pickupLatLng = LatLng(pLat, pLng);
    dropLatLng = LatLng(dLat, dLng);

    var pos = await LocationHelper.getCurrentLocation();
    if (pos!= null) driverLatLng = LatLng(pos.latitude, pos.longitude);

    setState(() {
      markers.add(Marker(markerId: const MarkerId('pickup'), position: pickupLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueRed), infoWindow: InfoWindow(title: "Pickup")));
      markers.add(Marker(markerId: const MarkerId('drop'), position: dropLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueGreen), infoWindow: InfoWindow(title: "Drop")));
      if (driverLatLng!= null) {
        markers.add(Marker(markerId: const MarkerId('driver'), position: driverLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueBlue), infoWindow: const InfoWindow(title: "You")));
      }
      loading = false;
    });

    await _getRoadSecure();
  }

  Future<void> _getRoadSecure() async {
    if (pickupLatLng == null || dropLatLng == null) return;
    polylines.clear();
    try {
      if (driverLatLng!= null) {
        var route1 = await ApiService.getRouteFromBackend(origin: driverLatLng!, dest: pickupLatLng!);
        if (route1!= null && route1['points']!= null && route1['points'].toString().isNotEmpty) {
          var decoded1 = PolylinePoints().decodePolyline(route1['points']);
          var pts1 = decoded1.map((e) => LatLng(e.latitude, e.longitude)).toList();
          if (pts1.isNotEmpty) {
            polylines.add(Polyline(polylineId: const PolylineId('driver_to_pickup'), points: pts1, color: Colors.blue, width: 6, jointType: JointType.round));
          }
        } else {
          polylines.add(Polyline(polylineId: const PolylineId('driver_to_pickup_direct'), points: [driverLatLng!, pickupLatLng!], color: Colors.blue, width: 4, patterns: [PatternItem.dot, PatternItem.gap(10)]));
        }
      }
      var route2 = await ApiService.getRouteFromBackend(origin: pickupLatLng!, dest: dropLatLng!);
      if (route2!= null && route2['points']!= null && route2['points'].toString().isNotEmpty) {
        var decoded2 = PolylinePoints().decodePolyline(route2['points']);
        var pts2 = decoded2.map((e) => LatLng(e.latitude, e.longitude)).toList();
        if (pts2.isNotEmpty) {
          polylines.add(Polyline(polylineId: const PolylineId('pickup_to_drop'), points: pts2, color: Colors.black, width: 6, jointType: JointType.round));
          distanceText = "${route2['distance_text']?? ''} ${route2['duration_text']?? ''}";
        }
      } else {
        polylines.add(Polyline(polylineId: const PolylineId('pickup_to_drop_direct'), points: [pickupLatLng!, dropLatLng!], color: Colors.black, width: 6));
        distanceText = "Direct Route";
      }
      if (mounted) setState(() {});
    } catch (e) {
      print("Polyline Error $e");
    }
  }

  @override Widget build(BuildContext context) {
    String pickupAddr = widget.ride['pickup_address']?.toString()?? 'Pickup Address nahi mila';
    String dropAddr = widget.ride['drop_address']?.toString()?? 'Drop Address nahi mila';
    int seatsBooked = int.tryParse(widget.ride['seats_booked']?.toString()?? "1")?? 1;
    int totalSeats = int.tryParse(widget.ride['total_seats']?.toString()?? widget.ride['max_pool_seats']?.toString()?? "3")?? 3;
    bool isPool = widget.ride['trip_type']?.toString().toLowerCase().contains('pool')?? false;
    return Scaffold(
      appBar: AppBar(title: Text(distanceText + (isPool? " | Seats: $seatsBooked/$totalSeats" : ""), style: const TextStyle(fontSize: 12)), backgroundColor: Colors.green),
      body: loading? const Center(child: CircularProgressIndicator()) : Stack(children: [
        GoogleMap(initialCameraPosition: CameraPosition(target: pickupLatLng!, zoom: 12), markers: markers, polylines: polylines, myLocationEnabled: true, onMapCreated: (c) => mapController = c),
        Positioned(top: 10, left: 10, child: Container(padding: const EdgeInsets.all(8), decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(8), boxShadow: const [BoxShadow(color: Colors.black26, blurRadius: 4)]), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Row(children: [Container(width: 20, height: 4, color: Colors.blue), const SizedBox(width: 6), const Text("Driver से Pickup", style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold))]), const SizedBox(height: 4), Row(children: [Container(width: 20, height: 4, color: Colors.black), const SizedBox(width: 6), const Text("Pickup से Drop", style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold))])]))),
        Positioned(bottom: 20, left: 15, right: 15, child: Container(padding: const EdgeInsets.all(12), decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(12), boxShadow: const [BoxShadow(color: Colors.black26, blurRadius: 8)]), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Row(children: [const Icon(Icons.my_location, color: Colors.red, size: 18), const SizedBox(width: 6), Expanded(child: Text(pickupAddr, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)))]), const Divider(), Row(children: [const Icon(Icons.location_on, color: Colors.green, size: 18), const SizedBox(width: 6), Expanded(child: Text(dropAddr, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)))]), if(isPool) Padding(padding: EdgeInsets.only(top: 8), child: Text("💺 Total Seats (Ride Table): $totalSeats | Booked: $seatsBooked | Left: ${totalSeats - seatsBooked}", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 12, color: Colors.blue))),]))),
      ]),
    );
  }
}

class _ActiveRideWithMap extends StatefulWidget {
  final Map ride; final String driverId; final bool isOtpVerified; final TextEditingController otpController; final VoidCallback onVerifyOtp, onComplete, onCall, onHangup; final String callStatus;
  const _ActiveRideWithMap({required this.ride, required this.driverId, required this.isOtpVerified, required this.otpController, required this.onVerifyOtp, required this.onComplete, required this.onCall, required this.onHangup, required this.callStatus});
  @override State<_ActiveRideWithMap> createState() => _ActiveRideWithMapState();
}

class _ActiveRideWithMapState extends State<_ActiveRideWithMap> {
  GoogleMapController? mapController; LatLng? driverLatLng, pickupLatLng, dropLatLng; Set<Marker> markers = {}; Set<Polyline> polylines = {}; String driverToUserText = ""; String pickupToDropText = ""; StreamSubscription<Position>? _posStream; Timer? _timer; bool _isSecondRouteFetched = false;
  List _alongRouteRides = [];
  Timer? _alongTimer;

  @override void initState() {
    super.initState();
    _initMap();
    if(widget.ride['trip_type']?.toString().toLowerCase().contains('pool')?? false){
      _fetchAlongRouteRides();
      _alongTimer = Timer.periodic(Duration(seconds: 20), (_) => _fetchAlongRouteRides());
    }
  }
  @override void dispose() { _posStream?.cancel(); _timer?.cancel(); _alongTimer?.cancel(); super.dispose(); }

  Future<void> _fetchAlongRouteRides() async {
    try{
      var pos = await LocationHelper.getCurrentLocation();
      String url = '${AppConstants.baseUrl}/rides/pool/along-route/${widget.ride['id']}?driver_id=${widget.driverId}';
      if(pos!= null) url += '&driver_lat=${pos.latitude}&driver_lng=${pos.longitude}';
      final res = await http.get(Uri.parse(url)).timeout(Duration(seconds: 10));
      if(res.statusCode == 200){
        var list = json.decode(res.body) as List;
        if(mounted) setState(()=> _alongRouteRides = list);
      }
    }catch(e){}
  }

  Future<void> _initMap() async {
    pickupLatLng = LatLng(double.tryParse(widget.ride['pickup_lat'].toString())?? 0, double.tryParse(widget.ride['pickup_lng'].toString())?? 0);
    dropLatLng = LatLng(double.tryParse(widget.ride['drop_lat'].toString())?? 0, double.tryParse(widget.ride['drop_lng'].toString())?? 0);
    Position? pos = await LocationHelper.getCurrentLocation(); if (pos!= null) driverLatLng = LatLng(pos.latitude, pos.longitude);
    _updateMarkers(); _getBothRoadsSecure();
    _posStream = Geolocator.getPositionStream(locationSettings: const LocationSettings(accuracy: LocationAccuracy.high, distanceFilter: 15)).listen((p) { driverLatLng = LatLng(p.latitude, p.longitude); _updateMarkers(); if (mounted) setState(() {}); ApiService.updateDriverLocation(driverId: widget.driverId, lat: p.latitude, lng: p.longitude); });
    _timer = Timer.periodic(const Duration(seconds: 45), (_) => _getBothRoadsSecure());
  }
  void _updateMarkers() {
    if (driverLatLng == null || pickupLatLng == null || dropLatLng == null) return;
    markers = {
      Marker(markerId: const MarkerId('driver'), position: driverLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueBlue)),
      Marker(markerId: const MarkerId('pickup'), position: pickupLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueRed)),
      Marker(markerId: const MarkerId('drop'), position: dropLatLng!, icon: BitmapDescriptor.defaultMarkerWithHue(BitmapDescriptor.hueGreen))
    };
  }
  Future<void> _getBothRoadsSecure() async {
    if (driverLatLng == null || pickupLatLng == null || dropLatLng == null) return;
    try {
      var route1 = await ApiService.getRouteFromBackend(origin: driverLatLng!, dest: pickupLatLng!);
      List<LatLng> pts1 = []; String d1 = driverToUserText;
      if (route1!= null && route1['points']!= null) {
        d1 = "${route1['distance_text']?? ''} ${route1['duration_text']?? ''}";
        var decoded1 = PolylinePoints().decodePolyline(route1['points']);
        pts1 = decoded1.map((e) => LatLng(e.latitude, e.longitude)).toList();
      }
      List<LatLng> pts2 = []; String d2 = pickupToDropText;
      if (!_isSecondRouteFetched) {
        var route2 = await ApiService.getRouteFromBackend(origin: pickupLatLng!, dest: dropLatLng!);
        if (route2!= null && route2['points']!= null) {
          d2 = "${route2['distance_text']?? ''} ${route2['duration_text']?? ''}";
          var decoded2 = PolylinePoints().decodePolyline(route2['points']);
          pts2 = decoded2.map((e) => LatLng(e.latitude, e.longitude)).toList();
          _isSecondRouteFetched = true;
        }
      }
      if (mounted) {
        setState(() {
          driverToUserText = d1; pickupToDropText = d2;
          polylines.clear();
          if (pts1.isNotEmpty) polylines.add(Polyline(polylineId: const PolylineId('driver_to_user'), points: pts1, color: Colors.blue, width: 6, geodesic: true));
          if (pts2.isNotEmpty) polylines.add(Polyline(polylineId: const PolylineId('pickup_to_drop'), points: pts2, color: Colors.black, width: 6, geodesic: true));
          if (pts1.isEmpty && driverLatLng!= null) polylines.add(Polyline(polylineId: const PolylineId('driver_to_user_direct'), points: [driverLatLng!, pickupLatLng!], color: Colors.blue, width: 4, patterns: [PatternItem.dot, PatternItem.gap(10)]));
          if (pts2.isEmpty) polylines.add(Polyline(polylineId: const PolylineId('pickup_to_drop_direct'), points: [pickupLatLng!, dropLatLng!], color: Colors.black, width: 4));
        });
      }
    } catch (e) {}
  }
  @override Widget build(BuildContext context) {
    bool isParcel = widget.ride['trip_type']?.toString().toLowerCase().contains('parcel')?? false;
    bool isPool = widget.ride['trip_type']?.toString().toLowerCase().contains('pool')?? false;
    String curr = widget.ride['currency']?.toString()?? '₹';
    String tripLabel = isPool? "👥 POOL" : isParcel? "📦 PARCEL" : "🚗 RIDE";
    int seatsBooked = int.tryParse(widget.ride['seats_booked']?.toString()?? "1")?? 1;
    int totalSeats = int.tryParse(widget.ride['total_seats']?.toString()?? widget.ride['max_pool_seats']?.toString()?? "3")?? 3;
    return Column(children: [Expanded(flex: 5, child: Stack(children: [GoogleMap(initialCameraPosition: CameraPosition(target: driverLatLng?? pickupLatLng!, zoom: 15), markers: markers, polylines: polylines, myLocationEnabled: true, onMapCreated: (c) => mapController = c), Positioned(top: 10, left: 10, right: 10, child: (driverToUserText.isEmpty && pickupToDropText.isEmpty)? const SizedBox.shrink() : Container(padding: const EdgeInsets.all(10), decoration: BoxDecoration(color: Colors.black87, borderRadius: BorderRadius.circular(10)), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [if (driverToUserText.isNotEmpty) Text('🔵 Driver से Pickup: $driverToUserText', style: const TextStyle(color: Colors.white, fontSize: 12, fontWeight: FontWeight.bold)), if (pickupToDropText.isNotEmpty) Text('⚫ Pickup से Drop: $pickupToDropText', style: const TextStyle(color: Colors.white, fontSize: 12, fontWeight: FontWeight.bold))])))] )), Expanded(flex: 5, child: Container(padding: const EdgeInsets.all(12), color: Colors.white, child: SingleChildScrollView(child: Column(children: [Container(padding: const EdgeInsets.all(10), decoration: BoxDecoration(color: isPool? Colors.blue.shade50 : isParcel? Colors.orange.shade50 : Colors.green.shade50, borderRadius: BorderRadius.circular(10)), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text('$tripLabel #${widget.ride['id']} - $curr${widget.ride['fare']} ${isPool? " | Group: ${widget.ride['pool_group_id']}" : ""} | Seats: $seatsBooked/$totalSeats', style: const TextStyle(fontWeight: FontWeight.bold)), Text('📍 ${widget.ride['pickup_address']}'), Text('🏁 ${widget.ride['drop_address']}')])),
      const SizedBox(height: 10),
      if(isPool && _alongRouteRides.isNotEmpty)
        Container(
          margin: EdgeInsets.only(bottom: 10),
          padding: EdgeInsets.all(10),
          decoration: BoxDecoration(color: Colors.blue.shade50, borderRadius: BorderRadius.circular(10), border: Border.all(color: Colors.blue)),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [Icon(Icons.route, color: Colors.blue, size: 18), SizedBox(width: 6), Text("रास्ते में ${_alongRouteRides.length} Pool Rides!", style: TextStyle(fontWeight: FontWeight.bold, color: Colors.blue, fontSize: 13))]),
            SizedBox(height: 8),
          ..._alongRouteRides.map((r) => Card(margin: EdgeInsets.only(bottom: 6), child: ListTile(
              dense: true,
              title: Text("${r['pickup_address']}", maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 12, fontWeight: FontWeight.bold)),
              subtitle: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text("Drop: ${r['drop_address']}", maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 10)),
                Text("${r['distance_from_driver']}km आगे | ₹${r['fare']} | Seats:${r['seats_booked']}/${r['total_seats']}", style: TextStyle(fontSize: 11, color: Colors.grey[700])),
              ]),
              trailing: ElevatedButton(
                style: ElevatedButton.styleFrom(backgroundColor: Colors.blue, minimumSize: Size(55, 28), padding: EdgeInsets.symmetric(horizontal: 8)),
                onPressed: () async {
                  final res = await http.put(Uri.parse('${AppConstants.baseUrl}/rides/${r['id']}/accept-pool?driver_id=${widget.driverId}&group_id=${widget.ride['pool_group_id']}')).timeout(Duration(seconds: 10));
                  if(res.statusCode == 200){ ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text("Pool Ride Added! ${r['pickup_address']}"))); _fetchAlongRouteRides(); }
                },
                child: Text("Pick", style: TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.bold)),
              ),
            ))).toList()
          ]),
        ),
      Row(children: [Expanded(child: ElevatedButton.icon(onPressed: widget.onCall, icon: const Icon(Icons.call), label: const Text('Call'), style: ElevatedButton.styleFrom(backgroundColor: Colors.green))), const SizedBox(width: 8), Expanded(child: ElevatedButton.icon(onPressed: widget.onHangup, icon: const Icon(Icons.call_end), label: const Text('Cut'), style: ElevatedButton.styleFrom(backgroundColor: Colors.red)))]), Text(widget.callStatus, style: const TextStyle(fontWeight: FontWeight.bold)), const SizedBox(height: 8), if (!widget.isOtpVerified)...[TextField(controller: widget.otpController, keyboardType: TextInputType.number, maxLength: 6, decoration: const InputDecoration(hintText: 'Enter OTP', border: OutlineInputBorder(), counterText: '')), const SizedBox(height: 6), SizedBox(width: double.infinity, child: ElevatedButton(onPressed: widget.onVerifyOtp, style: ElevatedButton.styleFrom(backgroundColor: Colors.orange), child: const Text('Verify OTP', style: TextStyle(color: Colors.white))))] else...[SizedBox(width: double.infinity, child: ElevatedButton.icon(onPressed: widget.onComplete, icon: const Icon(Icons.check), label: Text('Complete $tripLabel'), style: ElevatedButton.styleFrom(backgroundColor: Colors.blue)))]])) ))]);
  }
}

