namespace ROMarr.Desktop;

/// <summary>Accepts server origins without credentials, paths, queries or script schemes.</summary>
internal static class RemoteServerUri
{
    /// <summary>Validates a server address and returns its normalized HTTP(S) origin.</summary>
    internal static string Normalize(string value)
    {
        if (!Uri.TryCreate(value.Trim(), UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttps && uri.Scheme != Uri.UriSchemeHttp)
            || string.IsNullOrEmpty(uri.Host) || !string.IsNullOrEmpty(uri.UserInfo)
            || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment)
            || uri.AbsolutePath != "/")
            throw new ArgumentException("Enter your ROMarr server address, such as https://romarr.example.com or http://192.168.1.20:6868. Leave out passwords, API keys and page paths.");
        return uri.GetLeftPart(UriPartial.Authority);
    }
}
