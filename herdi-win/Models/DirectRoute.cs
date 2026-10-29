using System.Text.Json;

namespace Herdi.Models;

/// <summary>Immutable routing captured with a pane. Labels never participate in routing.</summary>
public sealed record DirectRoute(string Kind, string Target, string Session = "", string ProfileId = "", string Label = "")
{
    public string Key => JsonSerializer.Serialize(new[] { Kind, ProfileId, Session, Target });
    public string DisplayLabel => Kind == "local" ? "local" : Label.Length > 0 ? Label : Target;
    public static DirectRoute Local { get; } = new("local", "");
    public static DirectRoute Ssh(string target) => new("ssh", target);
}
