function handler(event) {
    var pastries = [
        "apple_pie",
        "apple_pie_slice",
        "chocolate_chip_cupcake",
        "strawberry_cupcake",
        "blueberry_jelly_doughnut",
        "plain_bagel"
    ];
    var response = event.response;
    var expires = new Date();
    expires.setTime(+expires + 365 * 86400000);
    response.cookies = response.cookies || {};
    response.cookies.the_image = {
        value: pastries[Math.floor(Math.random() * pastries.length)],
        attributes: "Secure; Path=/; Domain=" + event.request.headers.host.value + "; Expires=" + expires.toUTCString() + ";"
    };
    return response;
}
