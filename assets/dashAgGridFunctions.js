var dagfuncs = (window.dashAgGridFunctions =
    window.dashAgGridFunctions || {});

dagfuncs.formatNumber = function (value) {
    if (value == null) {
        return "";
    }
    return Intl.NumberFormat("en-US", {
        maximumFractionDigits: 20,
    }).format(value);
};
