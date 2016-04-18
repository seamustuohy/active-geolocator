<?php
include("config.php");

function error_handler($errno, $errstr, $errfile, $errline) {
    http_response_code(500);
    header("Content-Type: text/plain;charset=utf-8");
    echo "Internal error: $errstr\n";
    exit(1);
}
error_reporting(0);
set_error_handler("error_handler");

$fp = fopen($LANDMARKS, "r");
$data = [];
while (($row = fgetcsv($fp)) !== FALSE) {
    $data[] = [$row[0], $row[1] + 0];
}
fclose($fp);

# In addition, we ask the client to ping 127.0.0.1, its apparent
# external IP address, a guess at its gateway address (last component
# of the IPv4 address forced to .1) and this server.  We use TCP port 7
# (echo protocol) for these, precisely because nobody runs an echo
# responder anymore.
$data[] = ["127.0.0.1", 7];

$client = $_SERVER["REMOTE_ADDR"];
# in the unlikely event of an ipv6 address, don't bother; the client
# can't handle them
if (preg_match("/^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$/", $client)) {
    $gw = preg_replace("/\.[0-9]{1,3}$/", ".1", $client);
    $data[] = [$client, 7];
    if ($client !== $gw) {
        $data[] = [$gw, 7];
    }
}
$data[] = [gethostbyname($_SERVER["HTTP_HOST"]), 7];

$blob = json_encode($data);
if ($blob === FALSE) {
    trigger_error(json_last_error_msg(), E_USER_ERROR);
} else {
    header("Content-Type: application/json;charset=utf-8");
    echo $blob;
}
?>