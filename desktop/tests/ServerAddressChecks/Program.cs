using ROMarr.Desktop;
var good = new Dictionary<string,string> { [" https://romarr.example.com/ "]="https://romarr.example.com", ["http://192.168.1.20:6868"]="http://192.168.1.20:6868", ["http://[::1]:6868/"]="http://[::1]:6868" };
foreach (var item in good) if (RemoteServerUri.Normalize(item.Key) != item.Value) throw new Exception("Origin normalization failed");
var bad = new[] { "javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com", "https://example.com/?apikey=secret", "https://example.com/#queue", "https://example.com/path", "not-a-server", "" };
foreach (var input in bad) { try { RemoteServerUri.Normalize(input); } catch (ArgumentException) { continue; } throw new Exception("Accepted invalid server origin"); }
Console.WriteLine("11 native server-address checks passed");
